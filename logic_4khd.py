import requests
from bs4 import BeautifulSoup
import re
import json
from urllib.parse import urljoin, urlparse

class Logic4KHD:
    BASE_URL = "https://gghh.uk"
    API_URL = f"{BASE_URL}/wp-json/wp/v2/posts"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    }

    @staticmethod
    def get_session():
        try:
            import cloudscraper
            # 특정 브라우저 환경 모방 강화
            return cloudscraper.create_scraper(
                browser={
                    'browser': 'chrome',
                    'platform': 'windows',
                    'desktop': True
                }
            )
        except:
            return requests.Session()

    @staticmethod
    def normalize_image_url(url, for_thumbnail=False):
        """이미지 URL 정규화 (Tachiyomi 로직 반영)"""
        if not url: return url
        url = url.replace('\\/', '/').replace('&amp;', '&').strip()
        
        # Jetpack 프록시 우회 및 CDN 매핑
        # pic.4khd.com -> img.uuss.uk (원본), img.4khd.com (썸네일)
        if 'pic.4khd.com' in url:
            mapped_url = url
            if 'i0.wp.com/' in url: mapped_url = url.split('i0.wp.com/')[-1]
            elif 'i1.wp.com/' in url: mapped_url = url.split('i1.wp.com/')[-1]
            elif 'i2.wp.com/' in url: mapped_url = url.split('i2.wp.com/')[-1]
            
            target_host = "img.4khd.com" if for_thumbnail else "img.uuss.uk"
            mapped_url = mapped_url.replace('pic.4khd.com', target_host)
            if not mapped_url.startswith('http'):
                mapped_url = 'https://' + mapped_url
            return mapped_url
            
        return url

    @staticmethod
    def _discover_url(url):
        """4khd.com에서 실제 게시글/페이지 이동에 쓰이는 호스트를 찾습니다."""
        if not url:
            return Logic4KHD.BASE_URL
        if '4khd.com' not in url:
            return url
        try:
            session = Logic4KHD.get_session()
            res = session.get(url, headers=Logic4KHD.HEADERS, timeout=10, allow_redirects=True)
            if res.status_code != 200:
                return Logic4KHD.BASE_URL

            candidates = Logic4KHD._extract_discovery_candidates(res.text, res.url)
            for candidate in candidates:
                try:
                    moved = session.get(candidate, headers=Logic4KHD.HEADERS, timeout=10, allow_redirects=True)
                    if moved.status_code == 200:
                        final_base = Logic4KHD._base_from_url(moved.url)
                        if final_base and '4khd.com' not in final_base:
                            return final_base
                except:
                    continue

            final_url = Logic4KHD._base_from_url(res.url)
            return final_url or url
        except: pass
        return Logic4KHD.BASE_URL

    @staticmethod
    def is_supported_base_url(url):
        parsed = urlparse(url or '')
        host = parsed.netloc.lower()
        return (
            host == '4khd.com' or
            host.endswith('.4khd.com') or
            host == 'gghh.uk' or
            host.endswith('.gghh.uk')
        )

    @staticmethod
    def discover_url(url=None):
        """Return the currently reachable 4KHD mirror URL after redirects."""
        start_url = url or "https://4khd.com"
        return Logic4KHD._discover_url(start_url)

    @staticmethod
    def _base_from_url(url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _extract_discovery_candidates(html, base_url):
        soup = BeautifulSoup(html or '', 'html.parser')
        candidates = []

        selectors = [
            'a[href*="query-3-page=2"]',
            'a.page-numbers[href]',
            '.wp-block-post-title a[href]',
            'h2 a[href]',
            'h3 a[href]',
            '.entry-title a[href]',
            'article a[href]',
        ]
        for selector in selectors:
            for tag in soup.select(selector):
                href = tag.get('href')
                candidate = Logic4KHD._safe_discovery_candidate(href, base_url)
                if candidate:
                    candidates.append(candidate)

        candidates.append(urljoin(base_url, '/?query-3-page=2'))
        candidates.append(urljoin(base_url, '/page/2/'))
        candidates.append('https://gghh.uk/')

        unique = []
        for candidate in candidates:
            parsed = urlparse(candidate)
            if parsed.scheme in ['http', 'https'] and candidate not in unique:
                unique.append(candidate)
        return unique

    @staticmethod
    def _safe_discovery_candidate(href, base_url):
        if not href:
            return None

        candidate = urljoin(base_url, href)
        parsed = urlparse(candidate)
        if parsed.scheme not in ['http', 'https'] or not parsed.netloc:
            return None

        host = parsed.netloc.lower()
        base_host = urlparse(base_url).netloc.lower()
        path = parsed.path.lower()
        query = parsed.query.lower()

        if host == base_host or host.endswith('.4khd.com') or host == '4khd.com':
            return candidate

        if host in ['gghh.uk'] or host.endswith('.gghh.uk'):
            return candidate

        if any(token in path for token in ['/content/', '/page/']) or 'query-3-page' in query:
            return candidate

        return None

    @staticmethod
    def get_list(base_url=None, page=1, search="", category=""):
        """게시물 목록을 가져옵니다 (리다이렉트 자동 대응)"""
        # 1. 실제 접속할 미러 주소 파악 (설정값이 없으면 기본 대문 주소 사용)
        start_url = base_url if base_url and 'http' in base_url else Logic4KHD.BASE_URL
        real_base = Logic4KHD._discover_url(start_url)
        
        try:
            # 카테고리 요청은 HTML 파싱이 더 정확함 (슬러그 변동 대응)
            if category:
                return Logic4KHD.parse_html_list(real_base, page, search, category)

            # 최신 목록은 API 시도
            api_url = f"{real_base.rstrip('/')}/wp-json/wp/v2/posts"
            session = Logic4KHD.get_session()
            params = {'page': page, 'per_page': 20, '_embed': 1, 'orderby': 'date'}
            if search: params['search'] = search
            
            response = session.get(api_url, params=params, headers=Logic4KHD.HEADERS, timeout=15)
            if response.status_code == 200:
                posts = response.json()
                results = []
                for post in posts:
                    item = {
                        'id': post['id'],
                        'title': post['title']['rendered'],
                        'url': post['link'],
                        'thumbnail': ''
                    }
                    try:
                        thumb = None
                        embedded = post.get('_embedded', {})
                        if 'wp:featuredmedia' in embedded:
                            thumb = embedded['wp:featuredmedia'][0]['source_url']
                        if not thumb:
                            img_urls = Logic4KHD.extract_images_from_html(post['content']['rendered'])
                            if img_urls: thumb = img_urls[0]
                        item['thumbnail'] = Logic4KHD.normalize_image_url(thumb, for_thumbnail=True)
                    except: pass
                    results.append(item)
                return results
            
            return Logic4KHD.parse_html_list(real_base, page, search, category)
        except:
            return Logic4KHD.parse_html_list(real_base, page, search, category)

    @staticmethod
    def extract_images_from_html(html):
        """HTML에서 이미지 URL 추출 (Lazy loading 대응)"""
        if not html: return []
        soup = BeautifulSoup(html, 'html.parser')
        images = []
        for img in soup.select('img'):
            src = img.get('data-src') or img.get('data-lazy-src') or img.get('srcset') or img.get('src')
            if src:
                if ',' in src: src = src.split(',')[0].split(' ')[0] # srcset 대응
                images.append(src)
        return images

    @staticmethod
    def parse_html_list(base_url, page=1, search="", category=""):
        """HTML 파싱을 통한 목록 추출 (페이징 완벽 대응)"""
        base_url = base_url.rstrip('/')
        
        # 주소 생성 (리다이렉트된 최종 도메인 기준)
        urls = []
        if search:
            if page > 1:
                # 다양한 검색 페이징 패턴 시도
                urls.append(f"{base_url}/page/{page}/?s={search}")
                urls.append(f"{base_url}/search/{search}/page/{page}/")
                urls.append(f"{base_url}/?s={search}&paged={page}")
                urls.append(f"{base_url}/?s={search}&page={page}")
            else:
                urls.append(f"{base_url}/search/{search}/")
                urls.append(f"{base_url}/?s={search}")
        elif category:
            if page > 1:
                urls.append(f"{base_url}/{category}/?query-3-page={page}")
                urls.append(f"{base_url}/{category}/page/{page}/")
                urls.append(f"{base_url}/{category}/?paged={page}")
            else:
                urls.append(f"{base_url}/{category}/")
        else:
            if page > 1:
                urls = [f"{base_url}/?query-3-page={page}", f"{base_url}/page/{page}/"]
            else:
                urls = [f"{base_url}/"]
        
        try:
            session = Logic4KHD.get_session()
            res = None
            for idx, url in enumerate(urls):
                try:
                    res = session.get(url, headers=Logic4KHD.HEADERS, timeout=12)
                    if res.status_code == 200:
                        # 페이지 튕김 방지 검증 (2페이지 이상 요청했는데 1페이지로 리다이렉트 되었는지 확인)
                        if page > 1 and not any(p in res.url for p in [f"page/{page}", f"paged={page}", "query-"]):
                            continue
                        break
                except: continue
            
            if not res or res.status_code != 200: return []

            soup = BeautifulSoup(res.text, 'html.parser')
            # 본문 게시물 선택자 강화
            container = soup.select_one('.wp-block-post-template, main, #primary, #content') or soup
            posts = container.select('li.wp-block-post, article.post, div.wp-block-post, article, .grid-item')
            
            results = []
            for post in posts:
                try:
                    title_tag = post.select_one('h2 a, h3 a, .entry-title a, .wp-block-post-title a')
                    if not title_tag: continue
                    
                    img_tag = post.select_one('img')
                    thumb = ''
                    if img_tag:
                        # 모든 지연 로딩 속성 탐색
                        thumb = (img_tag.get('data-src') or img_tag.get('data-lazy-src') or 
                                 img_tag.get('srcset') or img_tag.get('src', ''))
                        if ',' in thumb: thumb = thumb.split(',')[0].split(' ')[0]

                    item = {
                        'title': title_tag.get_text(strip=True),
                        'url': title_tag['href'],
                        'thumbnail': Logic4KHD.normalize_image_url(thumb, for_thumbnail=True),
                        'id': title_tag['href'].strip('/').split('/')[-1]
                    }
                    if not any(r['id'] == item['id'] for r in results):
                        results.append(item)
                except: continue
            return results
        except Exception as e:
            print(f"[4KHD] parse_html_list error: {e}")
            return []
        except Exception as e:
            print(f"[4KHD] parse_html_list error: {e}")
            return []

    @staticmethod
    def get_detail(url):
        """게시물 상세 정보 추출 (모든 페이지 자동 탐색)"""
        try:
            session = Logic4KHD.get_session()
            base_url = url.rstrip('/')
            
            def get_page_data(target_url):
                try:
                    res = session.get(target_url, headers=Logic4KHD.HEADERS, timeout=15)
                    if res.status_code == 200:
                        return BeautifulSoup(res.text, 'html.parser')
                except:
                    try:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        res = session.get(target_url, headers=Logic4KHD.HEADERS, timeout=15, verify=False)
                        if res.status_code == 200:
                            return BeautifulSoup(res.text, 'html.parser')
                    except: pass
                return None

            soup = get_page_data(url)
            if not soup: return None

            data = {
                'title': soup.select_one('.wp-block-post-title, .entry-title').get_text(strip=True),
                'images': [],
                'videos': [],
                'downloads': []
            }

            # 1. 페이지네이션 확인 (모든 페이지 주소 수집)
            page_urls = [url]
            # .page-links a, .post-page-numbers a 등 워드프레스 표준 셀렉터
            page_links = soup.select('.page-links a, .post-page-numbers a, .pagination a')
            for link in page_links:
                href = link.get('href')
                if href and href not in page_urls:
                    page_urls.append(href)
            
            # 페이지 번호 순서대로 정렬 (중복 제거 및 정렬)
            page_urls = sorted(list(set(page_urls)))

            # 2. 모든 페이지 순회하며 데이터 추출
            for p_url in page_urls:
                p_soup = soup if p_url == url else get_page_data(p_url)
                if not p_soup: continue

                content = p_soup.select_one('.entry-content, .wp-block-post-content') or p_soup.body
                if content:
                    # 이미지 추출
                    raw_images = Logic4KHD.extract_images_from_html(str(content))
                    for img_url in raw_images:
                        norm_url = Logic4KHD.normalize_image_url(img_url, for_thumbnail=False)
                        if norm_url not in data['images']:
                            data['images'].append(norm_url)

                    # 비디오 (첫 페이지에서 주로 발견되지만 혹시 모르니)
                    for iframe in content.select('iframe'):
                        src = iframe.get('src')
                        if src and src not in data['videos']:
                            data['videos'].append(src)

                    # 다운로드 (주로 마지막 페이지에 있음)
                    for a in content.select('a'):
                        href = a.get('href', '')
                        if any(domain in href for domain in ['terabox', 'mega.nz', 'pixeldrain']):
                            if not any(d['link'] == href for d in data['downloads']):
                                data['downloads'].append({
                                    'name': a.get_text(strip=True) or 'Download Link',
                                    'link': href
                                })

            return data
        except Exception as e:
            print(f"[4KHD] get_detail error: {type(e).__name__}")
            return None

if __name__ == "__main__":
    # 간단 테스트
    logic = Logic4KHD()
    print("Listing latest posts...")
    items = logic.get_list(page=1)
    for i in items[:3]:
        print(f"Title: {i['title']}")
        print(f"URL: {i['url']}")
        # detail = logic.get_detail(i['url'])
        # print(f"Images count: {len(detail['images']) if detail else 0}")
        print("-" * 20)
