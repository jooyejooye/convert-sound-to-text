"""
daininvest.co.kr 최신 글 자동 감지 + 음성 다운로드 + 텍스트 변환 + 텔레그램 전송 봇

흐름:
1. Playwright 헤드리스 브라우저로 사이트 로그인
2. 게시판 목록 페이지에서 "가장 최신 글" 하나만 확인
3. 마지막으로 처리한 글(last_seen.json에 저장)과 다르면 새 글로 판단
4. 새 글이면 들어가서 네트워크 요청을 가로채 오디오 파일 URL 수집 → 다운로드
5. faster-whisper(로컬, 무료)로 텍스트 변환
6. 텔레그램 봇으로 "글 제목 + 변환된 텍스트" 전송
7. 처리한 글 URL을 last_seen.json에 저장 (워크플로우에서 커밋해서 영구 보존)

※ 한 번 실행 사이에 글이 2개 이상 새로 올라와도 가장 최신 글 1개만 처리합니다.
  (여러 개를 다 처리하고 싶으면 다시 "여러 새 글 처리" 방식으로 바꿔달라고 요청하세요)

환경변수로 받는 값 (GitHub Actions Secrets에 저장):
  SITE_ID, SITE_PW, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

주의: 코드에 아이디/비밀번호를 절대 직접 적지 마세요. 반드시 환경변수/Secrets로 주입하세요.
"""

import os
import re
import sys
import json
import time
import requests
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright

# ---------- 설정값 ----------
SITE_URL = "https://www.daininvest.co.kr/"
LOGIN_URL = SITE_URL  # 실제 로그인 페이지 URL이 다르면 여기 수정

SITE_ID = os.environ["SITE_ID"]
SITE_PW = os.environ["SITE_PW"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ⚠️ 게시판 목록 페이지 URL로 교체하세요. (글 목록이 보이는 페이지)
LIST_URL = SITE_URL + "board/list"  # 예시, 실제 값으로 교체

# ⚠️ 목록에서 각 게시글로 가는 링크의 selector로 교체하세요.
# 보통 게시글 제목 <a> 태그들이 반복되는 구조입니다.
POST_LINK_SELECTOR = "a.board-list-title"  # 예시, 실제 값으로 교체

# ⚠️ 로그인 폼 selector
ID_INPUT_SELECTOR = "input[name='userid']"       # 예시, 실제 값으로 교체
PW_INPUT_SELECTOR = "input[name='userpw']"       # 예시, 실제 값으로 교체
LOGIN_BUTTON_SELECTOR = "button[type='submit']"  # 예시, 실제 값으로 교체

# ⚠️ 글 안에서 음성 재생 버튼 selector
PLAY_BUTTON_SELECTOR = "button.play-btn"  # 예시, 실제 값으로 교체

LAST_SEEN_FILE = Path("last_seen.json")

# 오디오로 추정되는 요청인지 판단하는 정규식 (필요하면 패턴 추가)
AUDIO_PATTERN = re.compile(r"\.(mp3|m4a|wav|aac|ogg)(\?|$)", re.IGNORECASE)


def load_last_seen_url() -> str | None:
    if LAST_SEEN_FILE.exists():
        data = json.loads(LAST_SEEN_FILE.read_text(encoding="utf-8"))
        return data.get("last_url")
    return None


def save_last_seen_url(url: str):
    LAST_SEEN_FILE.write_text(
        json.dumps({"last_url": url}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def login(page):
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.fill(ID_INPUT_SELECTOR, SITE_ID)
    page.fill(PW_INPUT_SELECTOR, SITE_PW)
    page.click(LOGIN_BUTTON_SELECTOR)
    page.wait_for_load_state("networkidle")


def get_latest_post(page) -> tuple:
    """게시판 목록 페이지에서 가장 최신 글 (제목, URL) 하나만 가져온다.
    ⚠️ 게시판이 "최신 글이 맨 위" 순서로 정렬되어 있다고 가정합니다.
    만약 반대 순서라면 아래에서 links[0] 대신 links[-1]로 바꾸세요."""
    page.goto(LIST_URL, wait_until="networkidle")
    links = page.query_selector_all(POST_LINK_SELECTOR)
    if not links:
        raise RuntimeError("게시판에서 글 링크를 찾지 못했습니다. POST_LINK_SELECTOR를 확인하세요.")

    first = links[0]  # 최신 글이 맨 위라고 가정
    href = first.get_attribute("href")
    title = (first.inner_text() or "").strip()
    full_url = urljoin(LIST_URL, href)
    return title, full_url


def find_audio_in_post(context, post_url: str, page_timeout_sec: int = 20) -> str:
    """게시글 페이지에 들어가서 재생 버튼을 눌러 오디오 요청 URL을 캡처한다."""
    captured = {}
    page = context.new_page()

    def on_response(response):
        url = response.url
        ctype = response.headers.get("content-type", "")
        if AUDIO_PATTERN.search(url) or "audio" in ctype:
            captured["url"] = url

    page.on("response", on_response)
    page.goto(post_url, wait_until="networkidle")

    try:
        page.click(PLAY_BUTTON_SELECTOR, timeout=5000)
    except Exception:
        print(f"[{post_url}] 재생 버튼 클릭 실패 - selector를 확인하세요.", file=sys.stderr)

    waited = 0
    while "url" not in captured and waited < page_timeout_sec:
        time.sleep(1)
        waited += 1

    page.close()

    if "url" not in captured:
        raise RuntimeError(f"[{post_url}] 오디오 요청을 찾지 못했습니다.")

    return captured["url"]


def download_audio(url: str, cookies: dict) -> Path:
    """세션 쿠키를 실어서 오디오 파일을 다운로드한다."""
    resp = requests.get(url, cookies=cookies, timeout=60)
    resp.raise_for_status()

    # content-type으로 확장자 추정
    ctype = resp.headers.get("content-type", "")
    ext = "mp3"
    for cand in ["mp3", "m4a", "wav", "aac", "ogg"]:
        if cand in ctype or cand in url.lower():
            ext = cand
            break

    out_path = Path(f"audio_downloaded.{ext}")
    out_path.write_bytes(resp.content)
    return out_path


def transcribe(audio_path: Path) -> str:
    """faster-whisper로 음성을 텍스트로 변환 (무료, 로컬 실행)."""
    from faster_whisper import WhisperModel

    # "small" 모델: 속도와 정확도의 무난한 균형. 더 정확하게 하려면 "medium"으로 변경 가능(느려짐).
    model = WhisperModel("small", device="cpu", compute_type="int8")

    segments, _info = model.transcribe(str(audio_path), language="ko")
    text = "".join(segment.text for segment in segments)
    return text.strip()


def send_to_telegram(text: str, title: str = ""):
    """텔레그램 봇으로 텍스트 전송. 4096자 제한에 맞춰 분할 전송."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    header = f"📄 {title}\n\n" if title else ""
    full_text = header + text
    chunk_size = 4000
    chunks = [full_text[i:i + chunk_size] for i in range(0, len(full_text), chunk_size)] or ["(변환된 텍스트가 비어있습니다)"]

    for i, chunk in enumerate(chunks, 1):
        prefix = f"[{i}/{len(chunks)}]\n" if len(chunks) > 1 else ""
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": prefix + chunk}, timeout=30)


def main():
    last_seen_url = load_last_seen_url()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("1) 로그인 중...")
        login(page)

        print("2) 게시판에서 최신 글 확인 중...")
        title, post_url = get_latest_post(page)
        print(f"최신 글: {title} ({post_url})")

        if post_url == last_seen_url:
            print("이미 처리한 글입니다 (새 글 없음). 종료합니다.")
            browser.close()
            return

        cookies = {c["name"]: c["value"] for c in context.cookies()}

        print(f"\n--- 새 글 처리 중: {title} ---")
        audio_url = find_audio_in_post(context, post_url)
        print(f"오디오 URL 발견: {audio_url}")

        audio_path = download_audio(audio_url, cookies)
        print(f"다운로드 완료: {audio_path}")

        text = transcribe(audio_path)
        print("변환 완료. 텔레그램으로 전송합니다.")

        send_to_telegram(text, title=title)
        audio_path.unlink(missing_ok=True)

        browser.close()

    # 모든 과정이 성공했을 때만 "확인한 글"로 기록 (중간에 실패하면 다음 실행 때 재시도)
    save_last_seen_url(post_url)
    print("\n완료! last_seen.json이 갱신되었습니다.")


if __name__ == "__main__":
    main()
