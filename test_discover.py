import sys, importlib.util

spec = importlib.util.spec_from_file_location("logic_4khd", "ff_4khd_auto/logic_4khd.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Logic4KHD = mod.Logic4KHD

for base in ["https://usti.uuss.uk", "https://lzesh.ssuu.uk"]:
    print(f"\n=== {base} ===")
    items = Logic4KHD.get_list(base_url=base, page=1, search="")
    if not items:
        print("  목록 없음!")
        continue
    item = items[0]
    print(f"  제목: {item['title']}")
    print(f"  URL: {item['url']}")
    print(f"  이미지 수 (API): {len(item.get('images', []))}")

    # get_detail 테스트
    detail = Logic4KHD.get_detail(item["url"])
    if detail:
        print(f"  get_detail 이미지 수: {len(detail.get('images', []))}")
        if detail.get("images"):
            session = Logic4KHD.get_session()
            r = session.get(detail["images"][0], headers=Logic4KHD.HEADERS, timeout=10)
            print(f"  첫 이미지 HTTP: {r.status_code} | {r.headers.get('Content-Type')} | {len(r.content)}bytes")
    else:
        print("  get_detail 실패!")
