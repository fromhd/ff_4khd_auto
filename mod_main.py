import json
import traceback
import urllib.parse
import requests
import os
import threading
import time
import concurrent.futures
from datetime import datetime
from flask import Response, jsonify, render_template, request, stream_with_context
from plugin import PluginModuleBase, default_route_socketio_module
from .logic_4khd import Logic4KHD


class ModuleMain(PluginModuleBase):
    template_prefix = "ff_4khd_auto"

    def __init__(self, P):
        super(ModuleMain, self).__init__(P, name="main")
        self.db_default = {
            '4khd_url': Logic4KHD.BASE_URL,
            'download_path': os.path.join(os.getcwd(), 'downloads', 'ff_4khd_auto'),
            'proxy_enabled': 'True',
            'scan_interval': '30',
            'auto_enabled': 'False',
            'max_concurrent': '5',
            'discord_webhook': '',
            'keywords_json': '[]',
            'history_json': '[]',
        }
        self.menu = {
            'keywords': '키워드',
            'history': '히스토리',
            'setting': '설정',
            'log': '로그',
        }
        self._scan_timer = None
        self._scan_running = False
        self._download_status = {}
        self._db_lock = threading.Lock()
        self._status_lock = threading.Lock()
        default_route_socketio_module(self)

    # ─── Helpers: JSON settings & Thread Safety ────────────────────────

    def _get_keywords(self):
        with self._db_lock:
            try:
                return json.loads(self.P.ModelSetting.get('keywords_json') or '[]')
            except:
                return []

    def _save_keywords(self, keywords):
        with self._db_lock:
            self._set_setting('keywords_json', json.dumps(keywords, ensure_ascii=False))

    def _get_history(self):
        with self._db_lock:
            try:
                return json.loads(self.P.ModelSetting.get('history_json') or '[]')
            except:
                return []

    def _save_history(self, history):
        with self._db_lock:
            self._set_setting('history_json', json.dumps(history, ensure_ascii=False))

    def _is_downloaded(self, post_id, url=None, title=None):
        history = self._get_history()
        slug = url.strip('/').split('/')[-1] if url else None
        for h in history:
            if h.get('status') != 'completed':
                continue
            h_id = str(h.get('post_id'))
            h_url = h.get('url', '')
            h_slug = h_url.strip('/').split('/')[-1] if h_url else ''
            if h_id == str(post_id):
                return True
            if url and h_url and (h_url == url or (slug and h_slug and slug == h_slug)):
                return True
            if title and h.get('title') and h.get('title') == title:
                return True
        return False

    def _set_setting(self, key, value):
        if hasattr(self.P.ModelSetting, 'set'):
            return self.P.ModelSetting.set(key, value)
        from werkzeug.datastructures import MultiDict

        class SettingRequest:
            method = 'POST'
            def __init__(self, form):
                self.form = form

        form = MultiDict(self.P.ModelSetting.to_dict())
        form[key] = value
        return self.P.ModelSetting.setting_save(SettingRequest(form))

    # ─── Menu ──────────────────────────────────────────────────────────

    def process_menu(self, sub, req):
        arg = self.P.ModelSetting.to_dict()
        arg['package_name'] = self.P.package_name

        if sub == 'keywords':
            arg['title'] = '키워드'
            arg['keywords'] = self._get_keywords()
            return render_template(f"{self.template_prefix}_keywords.html", arg=arg)
        elif sub == 'history':
            arg['title'] = '히스토리'
            return render_template(f"{self.template_prefix}_history.html", arg=arg)
        elif sub == 'setting':
            arg['title'] = '설정'
            return render_template(f"{self.template_prefix}_setting.html", arg=arg)
        elif sub == 'log':
            arg['title'] = '로그'
            return render_template("log.html", package=self.P.package_name)
        return render_template("sample.html", title=f"4KHD Auto - {sub}")

    # ─── AJAX ──────────────────────────────────────────────────────────

    def process_ajax(self, sub, req):
        try:
            if sub == 'setting_save':
                ret = self.P.ModelSetting.setting_save(self._setting_request_with_defaults(req))
                self._restart_timer()
                return jsonify(ret)

            if sub == 'discover_url':
                discovered_url = Logic4KHD.discover_url()
                self._set_setting('4khd_url', discovered_url)
                return jsonify({'ret': 'success', 'url': discovered_url})

            if sub == 'test_discord':
                webhook_url = req.form.get('url', '').strip()
                if not webhook_url:
                    return jsonify({'ret': 'error', 'msg': 'Webhook URL을 입력하세요.'})
                ok, msg = self._send_discord_test(webhook_url)
                if ok:
                    return jsonify({'ret': 'success', 'msg': '디스코드 테스트 메시지 전송 완료!'})
                return jsonify({'ret': 'error', 'msg': f'전송 실패: {msg}'})

            if sub == 'keyword_add':
                keyword = req.form.get('keyword', '').strip()
                if not keyword:
                    return jsonify({'ret': 'error', 'msg': '키워드를 입력하세요.'})
                keywords = self._get_keywords()
                if any(k['keyword'].lower() == keyword.lower() for k in keywords):
                    return jsonify({'ret': 'error', 'msg': '이미 등록된 키워드입니다.'})
                keywords.append({
                    'keyword': keyword,
                    'include_old': False,
                    'enabled': True,
                })
                self._save_keywords(keywords)
                return jsonify({'ret': 'success'})

            if sub == 'keyword_delete':
                keyword = req.form.get('keyword', '').strip()
                keywords = self._get_keywords()
                keywords = [k for k in keywords if k['keyword'].lower() != keyword.lower()]
                self._save_keywords(keywords)
                return jsonify({'ret': 'success'})

            if sub == 'keyword_toggle':
                keyword = req.form.get('keyword', '').strip()
                field = req.form.get('field', 'enabled')
                keywords = self._get_keywords()
                for k in keywords:
                    if k['keyword'].lower() == keyword.lower():
                        k[field] = not k.get(field, False)
                        break
                self._save_keywords(keywords)
                return jsonify({'ret': 'success'})

            if sub == 'keyword_list':
                return jsonify({'ret': 'success', 'data': self._get_keywords()})

            if sub == 'scan_keyword':
                keyword = req.form.get('keyword', '').strip()
                if not keyword:
                    return jsonify({'ret': 'error', 'msg': '키워드를 지정하세요.'})
                keywords = self._get_keywords()
                kw_obj = next((k for k in keywords if k['keyword'].lower() == keyword.lower()), None)
                if not kw_obj:
                    return jsonify({'ret': 'error', 'msg': '등록되지 않은 키워드입니다.'})
                threading.Thread(target=self._scan_single_keyword, args=(kw_obj,), daemon=True).start()
                return jsonify({'ret': 'success', 'msg': f'"{keyword}" 스캔을 시작합니다.'})

            if sub == 'scan_all':
                if self._scan_running:
                    return jsonify({'ret': 'error', 'msg': '스캔이 이미 진행 중입니다.'})
                threading.Thread(target=self._run_scan, daemon=True).start()
                return jsonify({'ret': 'success', 'msg': '전체 스캔을 시작합니다.'})

            if sub == 'history_list':
                page = int(req.form.get('page', 1))
                per_page = 50
                history = self._get_history()
                history.sort(key=lambda h: h.get('date', ''), reverse=True)
                total = len(history)
                start = (page - 1) * per_page
                end = start + per_page
                return jsonify({
                    'ret': 'success',
                    'data': history[start:end],
                    'total': total,
                    'page': page,
                    'total_pages': (total + per_page - 1) // per_page
                })

            if sub == 'history_delete':
                post_id = req.form.get('post_id', '')
                history = self._get_history()
                history = [h for h in history if str(h.get('post_id')) != str(post_id)]
                self._save_history(history)
                return jsonify({'ret': 'success'})

            if sub == 'history_clear':
                self._save_history([])
                return jsonify({'ret': 'success'})

            if sub == 'redownload':
                url = req.form.get('url', '')
                post_id = req.form.get('post_id', '')
                if not url:
                    return jsonify({'ret': 'error', 'msg': 'URL이 없습니다.'})
                base_target_path = self.P.ModelSetting.get('download_path')
                
                # 기존 히스토리에서 키워드 및 제목 조회
                history = self._get_history()
                hist_item = next((h for h in history if str(h.get('post_id')) == str(post_id)), {})
                kw = hist_item.get('keyword', '수동')
                saved_title = hist_item.get('title', '')

                safe_keyword = "".join([c for c in kw if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
                target_path = os.path.join(base_target_path, safe_keyword) if safe_keyword else base_target_path
                if not os.path.exists(target_path):
                    os.makedirs(target_path, exist_ok=True)

                # 재다운로드는 get_detail 폴백 사용 (기존 파일 덮어쓰기 force=True)
                threading.Thread(
                    target=self._download_images,
                    args=(post_id, kw, url, [], target_path, True),
                    kwargs={'title_override': saved_title},
                    daemon=True
                ).start()
                return jsonify({'ret': 'success', 'msg': '재다운로드를 시작합니다.'})

            if sub == 'download_status':
                with self._status_lock:
                    return jsonify({'ret': 'success', 'data': dict(self._download_status)})

        except Exception as e:
            self.P.logger.error(f"Exception:{str(e)}")
            self.P.logger.error(traceback.format_exc())
            return jsonify({'ret': 'error', 'msg': str(e)})

    # ─── Auto-scan timer ───────────────────────────────────────────────

    def _restart_timer(self):
        if self._scan_timer:
            self._scan_timer.cancel()
            self._scan_timer = None

        auto_enabled = self.P.ModelSetting.get_bool('auto_enabled')
        if not auto_enabled:
            self.P.logger.info('[4KHD-Auto] 자동 스캔 비활성화됨')
            return

        interval = max(5, int(self.P.ModelSetting.get('scan_interval') or 30)) * 60

        def tick():
            self._run_scan()
            if self.P.ModelSetting.get_bool('auto_enabled'):
                self._scan_timer = threading.Timer(interval, tick)
                self._scan_timer.daemon = True
                self._scan_timer.start()

        self._scan_timer = threading.Timer(interval, tick)
        self._scan_timer.daemon = True
        self._scan_timer.start()
        self.P.logger.info(f'[4KHD-Auto] 자동 스캔 시작 (주기: {interval // 60}분)')

    def _run_scan(self):
        if self._scan_running:
            return
        self._scan_running = True
        try:
            self.P.logger.info('[4KHD-Auto] === 전체 스캔 시작 ===')
            keywords = self._get_keywords()
            for kw_obj in keywords:
                if kw_obj.get('enabled', True):
                    self._scan_single_keyword(kw_obj)
            self.P.logger.info('[4KHD-Auto] === 전체 스캔 완료 ===')
        except Exception as e:
            self.P.logger.error(f'[4KHD-Auto] 스캔 오류: {e}')
            self.P.logger.error(traceback.format_exc())
        finally:
            self._scan_running = False

    def _scan_single_keyword(self, kw_obj):
        keyword = kw_obj['keyword']
        include_old = kw_obj.get('include_old', False)
        self.P.logger.info(f'[4KHD-Auto] 키워드 스캔: "{keyword}" (과거포함: {include_old})')

        base_url = self._get_effective_base_url()
        base_target_path = self.P.ModelSetting.get('download_path')
        
        # 키워드별 서브폴더 경로 생성 (예: downloads/ff_4khd_auto/민하린/)
        safe_keyword = "".join([c for c in keyword if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
        target_path = os.path.join(base_target_path, safe_keyword) if safe_keyword else base_target_path
        if not os.path.exists(target_path):
            os.makedirs(target_path, exist_ok=True)

        page = 1
        empty_streak = 0
        max_pages = 200 if include_old else 3
        download_queue = []

        # 1단계: API 스캔 → 이미지 목록 포함 수집
        while page <= max_pages:
            try:
                self.P.logger.info(f'[4KHD-Auto] "{keyword}" 페이지 {page} 스캔...')
                items = Logic4KHD.get_list(base_url=base_url, page=page, search=keyword)
                if not items:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                    page += 1
                    time.sleep(1)
                    continue

                new_count = 0
                for item in items:
                    post_id = item.get('id', item.get('url', '').strip('/').split('/')[-1])
                    if keyword.lower() not in item.get('title', '').lower():
                        continue
                    item_url = item.get('url', '')
                    item_title = item.get('title', '')
                    if not self._is_downloaded(str(post_id), url=item_url, title=item_title):
                        download_queue.append({
                            'post_id': str(post_id),
                            'title': item_title,
                            'url': item_url,
                            'images': item.get('images', []),
                        })
                        new_count += 1

                if new_count == 0:
                    empty_streak += 1
                    if empty_streak >= 2 and not include_old:
                        break
                    if empty_streak >= 3 and include_old:
                        break
                else:
                    empty_streak = 0

                page += 1
                time.sleep(1)

            except Exception as e:
                self.P.logger.error(f'[4KHD-Auto] 페이지 {page} 스캔 오류: {e}')
                break

        self.P.logger.info(f'[4KHD-Auto] "{keyword}" 스캔 완료: {len(download_queue)}개 다운로드 대기')

        # 2단계: 병렬/동시 다운로드 (max_concurrent 설정 적용)
        if download_queue:
            try:
                max_concurrent = max(1, min(10, int(self.P.ModelSetting.get('max_concurrent') or 5)))
            except:
                max_concurrent = 5

            self.P.logger.info(f'[4KHD-Auto] 다운로드 시작 (동시 다운로드 수: {max_concurrent}, 저장 경로: {target_path})')
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
                futures = []
                for idx, item in enumerate(download_queue):
                    f = executor.submit(
                        self._download_images,
                        item['post_id'], keyword, item['url'],
                        item.get('images', []), target_path, False,
                        title_override=item.get('title', '')
                    )
                    futures.append(f)
                
                for f in concurrent.futures.as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        self.P.logger.error(f'[4KHD-Auto] 다운로드 작업 에러: {e}')

        # 과거 포함 스캔 완료 시 플래그 해제
        if include_old:
            keywords = self._get_keywords()
            for k in keywords:
                if k['keyword'].lower() == keyword.lower():
                    k['include_old'] = False
                    break
            self._save_keywords(keywords)
            self.P.logger.info(f'[4KHD-Auto] "{keyword}" 과거 자료 스캔 완료, include_old → False')

    # ─── Download ──────────────────────────────────────────────────────

    def _download_images(self, post_id, keyword, url, images, target_path, force=False, title_override=''):
        """이미지 목록을 다운로드하여 ZIP으로 묶고 히스토리에 기록.
        images가 비어있으면 get_detail 폴백 시도."""
        try:
            import zipfile
            import shutil
            import html
            from io import BytesIO
            try:
                from PIL import Image
            except:
                Image = None

            title = title_override or str(post_id)
            title = html.unescape(title).strip()

            # 이미지 목록이 없으면 get_detail 폴백 (재다운로드 등)
            if not images:
                self.P.logger.info(f'[4KHD-Auto] 이미지 목록 없음, get_detail 폴백: {post_id}')
                with self._status_lock:
                    self._download_status[post_id] = {'current': 0, 'total': 0, 'status': 'fetching'}
                data = None
                for attempt in range(3):
                    try:
                        data = Logic4KHD.get_detail(url)
                        if data and data.get('images'):
                            break
                    except:
                        pass
                    if attempt < 2:
                        if attempt == 1:
                            Logic4KHD.reset_session()
                        time.sleep((attempt + 1) * 3)

                if not data or not data.get('images'):
                    self._record_history(post_id, url, title, keyword, 'failed', '이미지 추출 실패')
                    with self._status_lock:
                        self._download_status[post_id] = {'status': 'error', 'msg': '이미지 추출 실패'}
                    return
                images = data['images']
                
                # title이 부실할 때만 detail 제목 사용
                detail_title = html.unescape(data.get('title', '')).strip()
                if (not title or title == str(post_id) or title.lower() == '4khd') and detail_title and detail_title.lower() != '4khd':
                    title = detail_title

            if not title or title.lower() == '4khd':
                title = f"{keyword}_{post_id}"

            self.P.logger.info(f'[4KHD-Auto] 다운로드 시작: {title} ({len(images)}장)')

            # 안전한 파일명 생성 (길이 제한 100자, 마침표/공백 정리)
            base_safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()[:100].rstrip('. ')
            if not base_safe_title or base_safe_title.lower() == '4khd':
                base_safe_title = f"{keyword}_{post_id}"
            
            zip_file_path = os.path.join(target_path, f"{base_safe_title}.zip")
            safe_title = base_safe_title

            # 중복 파일명 처리 (force가 아닌 경우 넘버링 추가)
            if not force:
                if os.path.exists(zip_file_path):
                    file_size = 0
                    try:
                        file_size = os.path.getsize(zip_file_path)
                    except:
                        pass
                    # 이미 정상 크기(100KB 이상) 파일이 존재하거나 히스토리에 완료 기록이 있으면 스킵
                    if file_size > 102400 or self._is_downloaded(str(post_id), url=url, title=title):
                        self.P.logger.info(f'[4KHD-Auto] 이미 정상 파일 존재하여 스킵: {zip_file_path} ({file_size} bytes)')
                        self._record_history(post_id, url, title, keyword, 'completed', zip_file_path)
                        return
                    elif file_size <= 102400:
                        # 손상되거나 비어있는 불완전한 파일(22바이트 등)은 삭제하고 재시도
                        try:
                            os.remove(zip_file_path)
                        except:
                            pass
                    else:
                        counter = 1
                        while os.path.exists(os.path.join(target_path, f"{base_safe_title} ({counter}).zip")):
                            counter += 1
                        safe_title = f"{base_safe_title} ({counter})"
                        zip_file_path = os.path.join(target_path, f"{safe_title}.zip")

            temp_folder = os.path.join(target_path, f"temp_{post_id}_{safe_title[:30]}")
            if not os.path.exists(temp_folder):
                os.makedirs(temp_folder, exist_ok=True)

            session = Logic4KHD.get_session()
            total = len(images)
            with self._status_lock:
                self._download_status[post_id] = {'current': 0, 'total': total, 'status': 'downloading', 'title': title}

            downloaded_files = []
            for idx, img_url in enumerate(images):
                for img_attempt in range(2):
                    try:
                        res = session.get(img_url, headers=Logic4KHD.HEADERS, timeout=30)
                        if res.status_code == 200:
                            content_type = res.headers.get('Content-Type', '')
                            # HTML 응답(Cloudflare 챌린지 등)이 오면 이미지가 아니므로 스킵
                            if 'text/html' in content_type:
                                self.P.logger.warning(f'[4KHD-Auto] 이미지 URL이 HTML 반환 (스킵): {img_url}')
                                break
                            file_path = os.path.join(temp_folder, f"{idx+1:03d}.jpg")
                            if Image and not img_url.lower().endswith('.gif'):
                                img = Image.open(BytesIO(res.content))
                                if img.mode != 'RGB':
                                    img = img.convert('RGB')
                                img.save(file_path, 'JPEG', quality=95, subsampling=0)
                            else:
                                with open(file_path, 'wb') as f:
                                    f.write(res.content)
                            downloaded_files.append(file_path)
                            break
                        elif res.status_code == 429 or res.status_code >= 500:
                            time.sleep(2)
                        else:
                            break
                    except:
                        if img_attempt == 0:
                            time.sleep(1)
                with self._status_lock:
                    if post_id in self._download_status:
                        self._download_status[post_id]['current'] = idx + 1

            # ZIP 압축
            if downloaded_files:
                with self._status_lock:
                    if post_id in self._download_status:
                        self._download_status[post_id]['status'] = 'zipping'
                with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for f in downloaded_files:
                        zipf.write(f, os.path.basename(f))

                shutil.rmtree(temp_folder, ignore_errors=True)
                with self._status_lock:
                    if post_id in self._download_status:
                        self._download_status[post_id]['status'] = 'completed'
                self._record_history(post_id, url, title, keyword, 'completed', zip_file_path)
                self.P.logger.info(f'[4KHD-Auto] ✅ 완료: {safe_title} ({len(downloaded_files)}장)')
                self._send_discord(
                    '✅ 다운로드 완료',
                    f'**{title}**\n키워드: `{keyword}`\n이미지: {len(downloaded_files)}장\n파일: `{safe_title}.zip`',
                    0x00FF00
                )
            else:
                with self._status_lock:
                    self._download_status[post_id] = {'status': 'error', 'msg': '이미지 다운 실패'}
                if os.path.exists(temp_folder):
                    shutil.rmtree(temp_folder, ignore_errors=True)
                self._record_history(post_id, url, title, keyword, 'failed', '이미지 다운로드 실패')
                self._send_discord('❌ 다운로드 실패', f'**{title}**\n키워드: `{keyword}`\n이미지 다운 실패', 0xFF0000)

        except Exception as e:
            # 에러 발생 시 불완전한 빈 ZIP 파일(22바이트 등) 삭제
            try:
                if os.path.exists(zip_file_path) and os.path.getsize(zip_file_path) < 10240:
                    os.remove(zip_file_path)
            except:
                pass
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder, ignore_errors=True)
            with self._status_lock:
                self._download_status[post_id] = {'status': 'error', 'msg': str(e)}
            self._record_history(post_id, url, title_override or str(post_id), keyword, 'failed', str(e))
            self.P.logger.error(f'[4KHD-Auto] 다운로드 오류: {e}')
            self.P.logger.error(traceback.format_exc())

    def _record_history(self, post_id, url, title, keyword, status, detail=''):
        with self._db_lock:
            try:
                history = json.loads(self.P.ModelSetting.get('history_json') or '[]')
            except:
                history = []
            existing = next((h for h in history if str(h.get('post_id')) == str(post_id)), None)
            entry = {
                'post_id': str(post_id),
                'url': url,
                'title': title,
                'keyword': keyword,
                'status': status,
                'detail': detail,
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            if existing:
                history = [entry if str(h.get('post_id')) == str(post_id) else h for h in history]
            else:
                history.insert(0, entry)
            self._set_setting('history_json', json.dumps(history, ensure_ascii=False))

    # ─── Discord Webhook ───────────────────────────────────────────────

    def _send_discord_test(self, webhook_url):
        try:
            payload = {
                'embeds': [{
                    'title': '📦 4KHD Auto — 테스트',
                    'description': '디스코드 알림이 정상적으로 작동합니다! ✅',
                    'color': 0x4fc3f7,
                    'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'footer': {'text': '4KHD Auto Downloader'}
                }]
            }
            res = requests.post(webhook_url, json=payload, timeout=10)
            if res.status_code in [200, 204]:
                return True, '성공'
            return False, f'HTTP {res.status_code}: {res.text[:100]}'
        except Exception as e:
            return False, str(e)

    def _send_discord(self, title, description, color=0x00FF00):
        webhook_url = self.P.ModelSetting.get('discord_webhook')
        if not webhook_url:
            return
        try:
            payload = {
                'embeds': [{
                    'title': f'📦 4KHD Auto — {title}',
                    'description': description,
                    'color': color,
                    'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'footer': {'text': '4KHD Auto Downloader'}
                }]
            }
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            self.P.logger.error(f'[4KHD-Auto] 디스코드 알림 실패: {e}')

    # ─── Utility ───────────────────────────────────────────────────────

    def _get_effective_base_url(self):
        base_url = self.P.ModelSetting.get('4khd_url') or Logic4KHD.BASE_URL
        # 저장된 URL이 유효하면 바로 사용 (매 요청마다 검증하지 않음)
        if base_url and 'http' in base_url:
            return base_url
        return Logic4KHD.BASE_URL

    def _make_proxy_url(self, target):
        return f"/{self.P.package_name}/normal/proxy?url={urllib.parse.quote(target)}"

    def _setting_request_with_defaults(self, req):
        from werkzeug.datastructures import MultiDict

        class RequestProxy:
            def __init__(self, original, form):
                self._original = original
                self.form = form
            def __getattr__(self, name):
                return getattr(self._original, name)

        form = MultiDict(req.form)
        for checkbox in ['proxy_enabled', 'auto_enabled']:
            if checkbox not in form:
                form[checkbox] = 'False'
        return RequestProxy(req, form)

    def process_normal(self, sub, req):
        if sub == 'proxy':
            return self._proxy(req)
        return "Not Found", 404

    def _proxy(self, req):
        target_url = req.args.get('url')
        if not target_url:
            return "No URL", 400
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': self.P.ModelSetting.get('4khd_url') or Logic4KHD.BASE_URL
        }
        try:
            session = Logic4KHD.get_session()
            res = None
            try:
                res = session.get(target_url, headers=headers, stream=True, timeout=10)
            except:
                try:
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    res = session.get(target_url, headers=headers, stream=True, timeout=10, verify=False)
                except Exception as e:
                    return str(e), 500
            if not res or res.status_code >= 400:
                return "Target Error", res.status_code if res else 500

            def generate():
                try:
                    for chunk in res.iter_content(chunk_size=1024 * 64):
                        yield chunk
                finally:
                    res.close()

            response = Response(
                stream_with_context(generate()),
                status=res.status_code,
                content_type=res.headers.get('Content-Type')
            )
            response.headers['Cache-Control'] = 'public, max-age=604800'
            return response
        except Exception as e:
            return str(e), 500

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def plugin_load(self):
        self.P.logger.info('[4KHD-Auto] 플러그인 로드됨')
        self._restart_timer()

    def plugin_unload(self):
        self.P.logger.info('[4KHD-Auto] 플러그인 언로드')
        if self._scan_timer:
            self._scan_timer.cancel()
            self._scan_timer = None
