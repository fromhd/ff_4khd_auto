import json
import traceback
import urllib.parse
import requests
import os
import threading
import time
from datetime import datetime
from flask import Response, jsonify, render_template, request, stream_with_context
from plugin import PluginModuleBase, default_route_socketio_module
from .logic_4khd import Logic4KHD


class ModuleMain(PluginModuleBase):
    template_prefix = "ff_4khd_auto"

    def __init__(self, P):
        super(ModuleMain, self).__init__(P, name="main")
        self.db_default = {
            '4khd_url': 'https://uuss.uk',
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
        default_route_socketio_module(self)

    # ─── Helpers: JSON settings ────────────────────────────────────────

    def _get_keywords(self):
        try:
            return json.loads(self.P.ModelSetting.get('keywords_json') or '[]')
        except:
            return []

    def _save_keywords(self, keywords):
        self._set_setting('keywords_json', json.dumps(keywords, ensure_ascii=False))

    def _get_history(self):
        try:
            return json.loads(self.P.ModelSetting.get('history_json') or '[]')
        except:
            return []

    def _save_history(self, history):
        self._set_setting('history_json', json.dumps(history, ensure_ascii=False))

    def _is_downloaded(self, post_id):
        history = self._get_history()
        return any(h['post_id'] == post_id and h['status'] == 'completed' for h in history)

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
                discovered_url = Logic4KHD.discover_url('https://4khd.com')
                self._set_setting('4khd_url', discovered_url)
                return jsonify({'ret': 'success', 'url': discovered_url})

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
                history = [h for h in history if h['post_id'] != post_id]
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
                target_path = self.P.ModelSetting.get('download_path')
                if not os.path.exists(target_path):
                    os.makedirs(target_path)
                # 재다운로드는 get_detail 폴백 사용 (이미지 목록 없으므로)
                threading.Thread(
                    target=self._download_images,
                    args=(post_id, '수동', url, [], target_path, True),
                    daemon=True
                ).start()
                return jsonify({'ret': 'success', 'msg': '재다운로드를 시작합니다.'})

            if sub == 'download_status':
                return jsonify({'ret': 'success', 'data': self._download_status})

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
        target_path = self.P.ModelSetting.get('download_path')
        if not os.path.exists(target_path):
            os.makedirs(target_path)

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
                    if not self._is_downloaded(str(post_id)):
                        download_queue.append({
                            'post_id': str(post_id),
                            'title': item.get('title', ''),
                            'url': item.get('url', ''),
                            'images': item.get('images', []),  # ★ API에서 이미 추출됨
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

        # 2단계: 순차 다운로드 (이미지 목록 직접 전달, get_detail 호출 없음)
        for idx, item in enumerate(download_queue):
            img_count = len(item.get('images', []))
            self.P.logger.info(f'[4KHD-Auto] [{idx+1}/{len(download_queue)}] {item["title"]} ({img_count}장)')
            self._download_images(
                item['post_id'], keyword, item['url'],
                item.get('images', []), target_path, False,
                title_override=item.get('title', '')
            )
            if idx < len(download_queue) - 1:
                time.sleep(2)

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
            from io import BytesIO
            try:
                from PIL import Image
            except:
                Image = None

            title = title_override or str(post_id)

            # 이미지 목록이 없으면 get_detail 폴백 (재다운로드 등)
            if not images:
                self.P.logger.info(f'[4KHD-Auto] 이미지 없음, get_detail 폴백: {post_id}')
                self._download_status[post_id] = {'current': 0, 'total': 0, 'status': 'fetching'}
                data = None
                for attempt in range(3):
                    try:
                        data = Logic4KHD.get_detail(url)
                        if data:
                            break
                    except:
                        pass
                    if attempt < 2:
                        if attempt == 1:
                            Logic4KHD.reset_session()
                        time.sleep((attempt + 1) * 5)

                if not data or not data.get('images'):
                    self._record_history(post_id, url, title, keyword, 'failed', '이미지 추출 실패')
                    self._download_status[post_id] = {'status': 'error', 'msg': '이미지 추출 실패'}
                    return
                images = data['images']
                title = data.get('title', title)

            self.P.logger.info(f'[4KHD-Auto] 다운로드 시작: {title} ({len(images)}장)')

            safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
            if not safe_title:
                safe_title = str(post_id)
            zip_file_path = os.path.join(target_path, f"{safe_title}.zip")

            # 중복 체크
            if not force and os.path.exists(zip_file_path):
                self.P.logger.info(f'[4KHD-Auto] 이미 존재: {zip_file_path}')
                self._record_history(post_id, url, title, keyword, 'completed', zip_file_path)
                return

            temp_folder = os.path.join(target_path, f"temp_{safe_title}")
            if not os.path.exists(temp_folder):
                os.makedirs(temp_folder)

            session = Logic4KHD.get_session()
            total = len(images)
            self._download_status[post_id] = {'current': 0, 'total': total, 'status': 'downloading', 'title': title}

            downloaded_files = []
            for idx, img_url in enumerate(images):
                for img_attempt in range(2):
                    try:
                        res = session.get(img_url, headers=Logic4KHD.HEADERS, timeout=30)
                        if res.status_code == 200:
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
                            time.sleep(3)
                        else:
                            break
                    except:
                        if img_attempt == 0:
                            time.sleep(2)
                self._download_status[post_id]['current'] = idx + 1

            # ZIP 압축
            if downloaded_files:
                self._download_status[post_id]['status'] = 'zipping'
                with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for f in downloaded_files:
                        zipf.write(f, os.path.basename(f))

                shutil.rmtree(temp_folder, ignore_errors=True)
                self._download_status[post_id]['status'] = 'completed'
                self._record_history(post_id, url, title, keyword, 'completed', zip_file_path)
                self.P.logger.info(f'[4KHD-Auto] ✅ 완료: {safe_title} ({len(downloaded_files)}장)')
                self._send_discord(
                    '✅ 다운로드 완료',
                    f'**{title}**\n키워드: `{keyword}`\n이미지: {len(downloaded_files)}장\n파일: `{safe_title}.zip`',
                    0x00FF00
                )
            else:
                self._download_status[post_id] = {'status': 'error', 'msg': '이미지 다운 실패'}
                if os.path.exists(temp_folder):
                    shutil.rmtree(temp_folder, ignore_errors=True)
                self._record_history(post_id, url, title, keyword, 'failed', '이미지 다운로드 실패')
                self._send_discord('❌ 다운로드 실패', f'**{title}**\n키워드: `{keyword}`\n이미지 다운 실패', 0xFF0000)

        except Exception as e:
            self._download_status[post_id] = {'status': 'error', 'msg': str(e)}
            self._record_history(post_id, url, title_override or str(post_id), keyword, 'failed', str(e))
            self.P.logger.error(f'[4KHD-Auto] 다운로드 오류: {e}')
            self.P.logger.error(traceback.format_exc())

    def _record_history(self, post_id, url, title, keyword, status, detail=''):
        history = self._get_history()
        existing = next((h for h in history if h['post_id'] == post_id), None)
        entry = {
            'post_id': post_id,
            'url': url,
            'title': title,
            'keyword': keyword,
            'status': status,
            'detail': detail,
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        if existing:
            history = [entry if h['post_id'] == post_id else h for h in history]
        else:
            history.insert(0, entry)
        self._save_history(history)

    # ─── Discord Webhook ───────────────────────────────────────────────

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
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                    'footer': {'text': '4KHD Auto Downloader'}
                }]
            }
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            self.P.logger.error(f'[4KHD-Auto] 디스코드 알림 실패: {e}')

    # ─── Utility ───────────────────────────────────────────────────────

    def _get_effective_base_url(self):
        base_url = self.P.ModelSetting.get('4khd_url') or Logic4KHD.BASE_URL
        # 저장된 URL이 유효하면 바로 사용 (discovery 불필요)
        if base_url and 'http' in base_url:
            return base_url
        # URL이 비정상이면 API 기반 탐지
        discovered_url = Logic4KHD.discover_url()
        self._set_setting('4khd_url', discovered_url)
        return discovered_url

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
