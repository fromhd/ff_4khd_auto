import requests
from bs4 import BeautifulSoup
import re
import json
import html
from urllib.parse import urljoin, urlparse

class Logic4KHD:
    BASE_URL = "https://uuss.uk"
    API_URL = f"{BASE_URL}/wp-json/wp/v2/posts"
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    }

    _shared_session = None

    @staticmethod
    def get_session(force_new=False):
        if force_new or Logic4KHD._shared_session is None:
            try:
                import cloudscraper
                Logic4KHD._shared_session = cloudscraper.create_scraper(
                    browser={
                        'browser': 'chrome',
                        'platform': 'windows',
                        'desktop': True
                    }
                )
            except:
                Logic4KHD._shared_session = requests.Session()
        return Logic4KHD._shared_session

    @staticmethod
    def reset_session():
        """세션 초기화 (Cloudflare 차단 시 새 세션 생성)"""
        Logic4KHD._shared_session = None

    @staticmethod
    def normalize_image_url(url, for_thumbnail=False):
        """이미지 URL 정규화 (WordPress Jetpack CDN 경유로 국내 차단 우회)"""
        if not url:
            return url
        url = url.replace('\\/', '/').replace('&amp;', '&').strip()

        # 4khd 이미지 도메인들은 국내에서 SNI 차단되므로 WordPress Jetpack CDN(i0.wp.com)으로 통일
        for blocked_host in ['img.uuss.uk', 'img.4khd.com', 'zy.4khd.com']:
            url = url.replace(blocked_host, 'pic.4khd.com')

        # 이미 i0.wp.com, i1.wp.com 등이 붙어있으면 그대로 사용
        if 'wp.com/' in url:
            if not url.startswith('http'):
                url = 'https://' + url.lstrip('/')
            return url

        if 'pic.4khd.com' in url:
            clean = re.sub(r'^https?://', '', url)
            return f"https://i0.wp.com/{clean}"

        return url

    @staticmethod
    def _verify_mirror(base_candidate, session):
        """미러 후보가 실제로 게시글 상세 페이지를 200으로 반환하는지 검증"""
        try:
            res = session.get(base_candidate, headers=Logic4KHD.HEADERS, timeout=8, allow_redirects=True)
            if res.status_code != 200:
                return None
            if 'location.href = url;' in res.text:
                return None
            if not ('wp-block-post-title' in res.text or 'nav-links' in res.text or 'popular' in res.text):
                return None

            soup = BeautifulSoup(res.text, 'html.parser')
            a = soup.select_one('li.wp-block-post h2 a, article h2 a, .entry-title a, .wp-block-post-title a')
            if not a or not a.get('href'):
                return Logic4KHD._base_from_url(res.url) or base_candidate

            post_href = a.get('href')
            parsed_href = urlparse(post_href)
            if '4khd.com' in parsed_href.netloc:
                target_detail = post_href.replace(f"{parsed_href.scheme}://{parsed_href.netloc}", base_candidate)
            else:
                target_detail = urljoin(base_candidate, post_href)

            det_res = session.get(target_detail, headers=Logic4KHD.HEADERS, timeout=8, allow_redirects=True)
            if det_res.status_code == 200 and 'location.href = url;' not in det_res.text:
                return Logic4KHD._base_from_url(base_candidate) or base_candidate
        except Exception:
            pass
        return None

    @staticmethod
    def _discover_url(url=None):
        """4khd 활성 미러 호스트를 찾습니다."""
        candidates = ["https://uuss.uk", "https://wrtm.ssuu.uk", "https://htpyz.uuss.uk", "https://gghh.uk", "https://4khd.com"]
        if url and url not in candidates:
            candidates.insert(0, url)

        session = Logic4KHD.get_session()
        html_to_parse = ""
        final_url = ""

        for u in candidates[:3]:
            try:
                res = session.get(u, headers=Logic4KHD.HEADERS, timeout=8, allow_redirects=True)
                if res.status_code == 200 and not html_to_parse:
                    html_to_parse = res.text
                    final_url = res.url
            except Exception:
                continue

        extracted_candidates = []
        if html_to_parse:
            extracted_candidates = Logic4KHD._extract_discovery_candidates(html_to_parse, final_url)

        all_candidates = []
        for c in extracted_candidates + candidates:
            base = Logic4KHD._base_from_url(c)
            if base and base not in all_candidates and '4khd.com' not in base and 'uuss.uk' != base:
                all_candidates.append(base)
        for c in candidates:
            base = Logic4KHD._base_from_url(c)
            if base and base not in all_candidates:
                all_candidates.append(base)

        for candidate in all_candidates:
            verified = Logic4KHD._verify_mirror(candidate, session)
            if verified:
                return verified

        return Logic4KHD.BASE_URL

    @staticmethod
    def discover_url(url=None):
        """Return the currently reachable 4KHD mirror URL after testing."""
        return Logic4KHD._discover_url(url)

    @staticmethod
    def _base_from_url(url):
        if not url: return None
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
            'h2 a[href]', 'h3 a[href]',
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

        for match in re.finditer(r"['\"](https?://[^'\"]+)['\"]", html):
            url = match.group(1)
            if any(x in url for x in ['uuss.uk', 'ssuu.uk', '4khd.com', 'gghh.uk']):
                candidate = Logic4KHD._safe_discovery_candidate(url, base_url)
                if candidate:
                    candidates.append(candidate)

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
        if host.endswith('.uuss.uk') or host == 'uuss.uk':
            return candidate
        if host.endswith('.ssuu.uk') or host == 'ssuu.uk':
            return candidate
        if host in ['gghh.uk'] or host.endswith('.gghh.uk'):
            return candidate
        if any(token in path for token in ['/content/', '/page/', '/pages/']) or 'query-3-page' in query:
            return candidate
        return None

    @staticmethod
    def get_list(base_url=None, page=1, search="", category=""):
        """게시물 목록 (WP REST API 우선, images 포함)"""
        start_url = base_url if base_url and 'http' in base_url else Logic4KHD.BASE_URL
        real_base = Logic4KHD._discover_url(start_url)

        try:
            if category:
                return Logic4KHD.parse_html_list(real_base, page, search, category)

            # WP REST API — content.rendered 에서 이미지 직접 추출
            api_url = f"{real_base.rstrip('/')}/wp-json/wp/v2/posts"
            session = Logic4KHD.get_session()
            params = {'page': page, 'per_page': 20, '_embed': 1, 'orderby': 'date'}
            if search:
                params['search'] = search

            response = session.get(api_url, params=params, headers=Logic4KHD.HEADERS, timeout=15)
            if response.status_code == 200:
                posts = response.json()
                results = []
                for post in posts:
                    content_html = ''
                    try:
                        content_html = post['content']['rendered']
                    except Exception:
                        pass

                    all_images = []
                    for img_url in Logic4KHD.extract_images_from_html(content_html):
                        norm = Logic4KHD.normalize_image_url(img_url, for_thumbnail=False)
                        if norm and norm not in all_images:
                            all_images.append(norm)

                    raw_title = post.get('title', {}).get('rendered', '')
                    clean_title = html.unescape(raw_title).strip()
                    item = {
                        'id': post['id'],
                        'title': clean_title or str(post['id']),
                        'url': post['link'],
                        'thumbnail': '',
                        'images': all_images,
                    }
                    try:
                        from urllib.parse import urlparse as _urlparse
                        parsed_link = _urlparse(item['url'])
                        if parsed_link.netloc and parsed_link.netloc != _urlparse(real_base).netloc:
                            item['url'] = item['url'].replace(
                                f"{parsed_link.scheme}://{parsed_link.netloc}", real_base.rstrip('/')
                            )
                    except Exception:
                        pass
                    try:
                        thumb = None
                        embedded = post.get('_embedded', {})
                        if 'wp:featuredmedia' in embedded:
                            thumb = embedded['wp:featuredmedia'][0]['source_url']
                        if not thumb and all_images:
                            thumb = all_images[0]
                        item['thumbnail'] = Logic4KHD.normalize_image_url(thumb, for_thumbnail=True)
                    except Exception:
                        pass
                    results.append(item)
                return results

            return Logic4KHD.parse_html_list(real_base, page, search, category)
        except Exception:
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
                if ',' in src: src = src.split(',')[0].split(' ')[0]
                images.append(src)
        return images

    @staticmethod
    def parse_html_list(base_url, page=1, search="", category=""):
        """HTML 파싱을 통한 목록 추출"""
        base_url = base_url.rstrip('/')
        urls = []
        if search:
            if page > 1:
                urls.append(f"{base_url}/page/{page}/?s={search}")
                urls.append(f"{base_url}/search/{search}/page/{page}/")
                urls.append(f"{base_url}/?s={search}&paged={page}")
                urls.append(f"{base_url}/?s={search}&page={page}")
            else:
                urls.append(f"{base_url}/search/{search}/")
                urls.append(f"{base_url}/?s={search}")
        elif category:
            if page > 1:
                urls.append(f"{base_url}/pages/{category}?query-3-page={page}")
                urls.append(f"{base_url}/{category}?query-3-page={page}")
                urls.append(f"{base_url}/pages/{category}/page/{page}/")
                urls.append(f"{base_url}/{category}/page/{page}/")
                urls.append(f"{base_url}/pages/{category}?paged={page}")
            else:
                urls.append(f"{base_url}/pages/{category}")
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
                        if page > 1 and not any(p in res.url for p in [f"page/{page}", f"paged={page}", "query-"]):
                            continue
                        break
                except Exception:
                    continue

            if not res or res.status_code != 200: return []

            soup = BeautifulSoup(res.text, 'html.parser')
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
                        thumb = (img_tag.get('data-src') or img_tag.get('data-lazy-src') or
                                 img_tag.get('srcset') or img_tag.get('src', ''))
                        if ',' in thumb: thumb = thumb.split(',')[0].split(' ')[0]

                    url_href = title_tag['href']
                    parsed_href = urlparse(url_href)
                    if '4khd.com' in parsed_href.netloc:
                        url_href = url_href.replace(parsed_href.scheme + '://' + parsed_href.netloc, base_url)

                    item = {
                        'title': title_tag.get_text(strip=True),
                        'url': url_href,
                        'thumbnail': Logic4KHD.normalize_image_url(thumb, for_thumbnail=True),
                        'id': url_href.strip('/').split('/')[-1],
                        'images': [],
                    }
                    if not any(r['id'] == item['id'] for r in results):
                        results.append(item)
                except Exception:
                    continue
            return results
        except Exception as e:
            print(f"[4KHD] parse_html_list error: {e}")
            return []

    @staticmethod
    def get_detail(url, session=None):
        """게시물 상세 정보 추출 (모든 페이지 자동 탐색 및 미러 404 시 자동 fallback)"""
        try:
            if session is None:
                session = Logic4KHD.get_session()

            parsed_input = urlparse(url)
            mirror_base = Logic4KHD.BASE_URL.rstrip('/')
            if '4khd.com' in parsed_input.netloc:
                url = url.replace(f"{parsed_input.scheme}://{parsed_input.netloc}", mirror_base)

            base_url = url.rstrip('/')

            def get_page_data(target_url):
                try:
                    res = session.get(target_url, headers=Logic4KHD.HEADERS, timeout=15)
                    if res.status_code == 200:
                        return BeautifulSoup(res.text, 'html.parser')
                    else:
                        print(f"[4KHD] get_page_data: HTTP {res.status_code} for {target_url}")
                except Exception as e1:
                    print(f"[4KHD] get_page_data failed: {type(e1).__name__}: {e1}")
                    try:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                        res = session.get(target_url, headers=Logic4KHD.HEADERS, timeout=15, verify=False)
                        if res.status_code == 200:
                            return BeautifulSoup(res.text, 'html.parser')
                    except Exception:
                        pass
                return None

            soup = get_page_data(url)
            # 만약 현재 base_url에서 404가 발생하면 최신 활성 미러로 URL 교체 후 재시도
            if not soup:
                active_base = Logic4KHD.discover_url()
                if active_base and active_base != base_url:
                    parsed_orig = urlparse(url)
                    fallback_url = url.replace(f"{parsed_orig.scheme}://{parsed_orig.netloc}", active_base)
                    soup = get_page_data(fallback_url)
                    if soup:
                        url = fallback_url
                        base_url = active_base

            if not soup:
                print(f"[4KHD] get_detail: failed to load page: {url}")
                return None

            # 제목 추출 로직 (사이트 로고 '4KHD' 오인식 방지)
            extracted_title = ''
            title_el = soup.select_one('.wp-block-post-title, .entry-title, h1.post-title, h1.entry-title, h2.entry-title')
            if title_el:
                t = title_el.get_text(strip=True)
                if t.lower() != '4khd' and len(t) > 3:
                    extracted_title = t

            if not extracted_title and soup.title:
                t = soup.title.get_text(strip=True)
                t = re.sub(r'\s*[-|_]\s*4KHD.*$', '', t, flags=re.IGNORECASE).strip()
                if t.lower() != '4khd' and len(t) > 3:
                    extracted_title = t

            if not extracted_title:
                for h1 in soup.select('h1, h2'):
                    t = h1.get_text(strip=True)
                    if t.lower() != '4khd' and len(t) > 3 and not any(kw in t.lower() for kw in ['popular', 'cosplay', 'album', 'how to download']):
                        extracted_title = t
                        break

            if not extracted_title:
                extracted_title = url.strip('/').split('/')[-1].replace('.html', '').replace('-', ' ')

            extracted_title = html.unescape(extracted_title).strip()

            data = {
                'title': extracted_title,
                'images': [],
                'videos': [],
                'downloads': []
            }

            page_urls = [url]
            page_links = soup.select('.page-links a, .post-page-numbers a, .pagination a')
            for link in page_links:
                href = link.get('href')
                if href:
                    parsed_href = urlparse(href)
                    if '4khd.com' in parsed_href.netloc:
                        href = href.replace(parsed_href.scheme + '://' + parsed_href.netloc, base_url)
                    if href not in page_urls:
                        page_urls.append(href)
            page_urls = sorted(list(set(page_urls)))

            for p_url in page_urls:
                p_soup = soup if p_url == url else get_page_data(p_url)
                if not p_soup: continue

                content = p_soup.select_one('.entry-content, .wp-block-post-content') or p_soup.body
                if content:
                    raw_images = Logic4KHD.extract_images_from_html(str(content))
                    for img_url in raw_images:
                        norm_url = Logic4KHD.normalize_image_url(img_url, for_thumbnail=False)
                        if norm_url not in data['images']:
                            data['images'].append(norm_url)

                    for iframe in content.select('iframe'):
                        src = iframe.get('src')
                        if src and src not in data['videos']:
                            data['videos'].append(src)

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
            print(f"[4KHD] get_detail error: {type(e).__name__}: {e}")
            return None

if __name__ == "__main__":
    logic = Logic4KHD()
    print("Listing latest posts...")
    items = logic.get_list(page=1)
    for i in items[:3]:
        print(f"Title: {i['title']}")
        print(f"URL: {i['url']}")
        print(f"Images: {len(i.get('images', []))}")
        print("-" * 20)
