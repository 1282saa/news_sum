# improved_news_mcp_server.py
import sys
import os
import httpx
import logging
import asyncio
import json
import re
from bs4 import BeautifulSoup
from pathlib import Path
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.prompts import base
from urllib.parse import urlparse
from datetime import datetime

# .env 파일 로드 (있는 경우)
env_path = Path(__file__).parent / '.env'
if env_path.exists():
    load_dotenv(env_path)

# 로깅 설정 개선
logging.basicConfig(
    stream=sys.stderr, 
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("news-context-analyzer")

# MCP 서버 생성
mcp = FastMCP("News Context Analyzer")

# Naver API 설정 - 환경변수에서 로드 (없으면 기본값 사용)
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "BRBkD_TaH9_cWnTRNDo0")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "nBQbi0IM30")

# 상수 정의
DEFAULT_TIMEOUT = 15.0  # 콘텐츠 추출을 위해 타임아웃 증가
MAX_RETRIES = 3
RETRY_DELAY = 1.0
MAX_NEWS_ITEMS = 20  # 검색할 최대 뉴스 항목 수 (10 -> 20으로 변경)
MAX_CONTENT_LENGTH = 1000  # 뉴스 내용의 최대 길이 (너무 길면 잘라냄)

NAVER_HEADERS = {
    "X-Naver-Client-Id": NAVER_CLIENT_ID,
    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
}

# 일반 HTTP 요청에 사용할 헤더
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache"
}

@mcp.tool()
def simple_test(text: str) -> str:
    """간단한 테스트 도구: 입력 텍스트를 그대로 반환합니다."""
    logger.info(f"Simple test called with: {text}")
    return f"입력 받은 텍스트: {text}"

async def make_request_with_retry(client, url, params=None, headers=None, max_retries=MAX_RETRIES):
    """재시도 로직이 포함된 HTTP 요청 함수"""
    for attempt in range(max_retries):
        try:
            response = await client.get(
                url, 
                params=params, 
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
                follow_redirects=True  # 리다이렉트 자동 처리
            )
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            logger.warning(f"Request timed out (attempt {attempt+1}/{max_retries})")
            if attempt < max_retries - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error: {e.response.status_code} - {e.response.text[:100]}")
            # 429 (Too Many Requests) 또는 5xx 오류만 재시도
            if (e.response.status_code == 429 or e.response.status_code >= 500) and attempt < max_retries - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise
        except Exception as e:
            logger.error(f"Request failed: {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                raise

def extract_publisher(item):
    """
    네이버 뉴스 API 응답에서 언론사 이름 추출
    """
    # 기사 제목에서 언론사 정보를 추출하려고 시도
    title = item.get("title", "")
    description = item.get("description", "")
    
    # 언론사 정보가 있는 일반적인 패턴 확인
    if " - " in title:
        return title.split(" - ")[-1].strip()
    
    # 링크에서 도메인 추출 시도
    link = item.get("link", "")
    if link:
        domain = urlparse(link).netloc
        # 도메인에서 언론사 이름 추출 시도
        if "news.naver.com" in domain:
            # 네이버 뉴스인 경우, 원본 언론사를 찾아야 함
            # 원본 출처는 description에 있을 수 있음
            if "출처 : " in description:
                return description.split("출처 : ")[1].strip()
        return domain.replace("www.", "")
    
    return "알 수 없는 언론사"

def format_date(date_str):
    """
    네이버 API 날짜 형식을 더 읽기 쉬운 형식으로 변환
    예: 'Mon, 06 May 2025 10:30:00 +0900' -> '2025-05-06 10:30'
    """
    try:
        # 네이버 API 날짜 형식 파싱
        dt = datetime.strptime(date_str, '%a, %d %b %Y %H:%M:%S %z')
        # 원하는 형식으로 변환
        return dt.strftime('%Y-%m-%d %H:%M')
    except Exception as e:
        logger.warning(f"날짜 형식 변환 실패: {e}")
        return date_str

async def extract_article_content(url, client):
    """
    뉴스 기사 URL에서 본문 내용 추출
    """
    try:
        response = await make_request_with_retry(client, url, headers=HTTP_HEADERS)
        html_content = response.text
        
        # BeautifulSoup을 사용하여 HTML 파싱
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 네이버 뉴스의 경우
        if "news.naver.com" in url:
            # 네이버 뉴스 본문은 일반적으로 다음 클래스 중 하나에 있음
            content_element = soup.select_one('#articleBodyContents, #articeBody, #newsEndContents, .news_end')
            if content_element:
                # 불필요한 요소 제거
                for element in content_element.select('script, style, .end_photo_org, .reporter_area'):
                    element.extract()
                
                content = content_element.get_text(strip=True)
                # 여러 개의 공백을 하나로 압축
                content = re.sub(r'\s+', ' ', content).strip()
                return content[:MAX_CONTENT_LENGTH] + ("..." if len(content) > MAX_CONTENT_LENGTH else "")
        
        # 다른 뉴스 사이트의 경우, 일반적인 방법으로 본문 추출 시도
        # article, main, p 태그 등 일반적인 뉴스 기사 구조 활용
        article_elements = soup.select('article, main, .article, .content, .news-content')
        if article_elements:
            content = article_elements[0].get_text(strip=True)
            content = re.sub(r'\s+', ' ', content).strip()
            return content[:MAX_CONTENT_LENGTH] + ("..." if len(content) > MAX_CONTENT_LENGTH else "")
        
        # 위 방법이 실패하면 p 태그 탐색
        paragraphs = soup.select('p')
        if paragraphs:
            content = ' '.join([p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 50])
            content = re.sub(r'\s+', ' ', content).strip()
            return content[:MAX_CONTENT_LENGTH] + ("..." if len(content) > MAX_CONTENT_LENGTH else "")
        
        return "본문 내용을 추출할 수 없습니다."
    
    except Exception as e:
        logger.error(f"기사 내용 추출 중 오류: {str(e)}")
        return f"기사 내용 추출 실패: {str(e)}"

@mcp.tool()
async def search_news(keyword: str, ctx: Context) -> str:
    """
    키워드로 뉴스 검색하기
    
    Args:
        keyword: 검색할 키워드
    
    Returns:
        검색된 뉴스 목록 (언론사, 제목, 시간, 링크 포함)
    """
    if not keyword or keyword.strip() == "":
        return "검색어를 입력해주세요."
        
    try:
        ctx.info(f"Searching for news about: {keyword}")
        
        # 키워드 검증 및 인코딩 처리
        keyword = keyword.strip()
        
        # 네이버 API로 검색 수행
        search_url = "https://openapi.naver.com/v1/search/news.json"
        params = {
            "query": keyword,
            "display": MAX_NEWS_ITEMS,  # 최대 20개 결과
            "sort": "sim"  # 유사도순 정렬
        }
        
        async with httpx.AsyncClient() as client:
            try:
                # 재시도 로직이 포함된 요청 함수 사용
                response = await make_request_with_retry(
                    client, 
                    search_url, 
                    params=params, 
                    headers=NAVER_HEADERS
                )
                
                # JSON 파싱 오류 처리
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    ctx.info(f"Invalid JSON response: {response.text[:100]}...")
                    return "검색 결과를 처리하는 중 오류가 발생했습니다: 잘못된 응답 형식"
                
                # 응답 검증
                if not isinstance(data, dict):
                    ctx.info(f"Unexpected response format: {type(data)}")
                    return "검색 결과를 처리하는 중 오류가 발생했습니다: 예상치 못한 응답 형식"
                
                if "items" not in data:
                    ctx.info(f"No 'items' in response: {data.keys()}")
                    return "검색 결과를 처리하는 중 오류가 발생했습니다: 항목을 찾을 수 없음"
                
                if not data["items"]:
                    return f"'{keyword}'에 대한 검색 결과가 없습니다."
                
                # 뉴스 정보 추출 및 구조화
                news_items = []
                for i, item in enumerate(data["items"]):
                    title = item.get("title", "제목 없음").replace("<b>", "").replace("</b>", "")
                    link = item.get("link", "#")
                    pub_date = format_date(item.get("pubDate", "날짜 정보 없음"))
                    publisher = extract_publisher(item)
                    
                    # 구조화된 형식으로 정보 추가
                    news_items.append(
                        f"{i+1}. [언론사] {publisher}\n"
                        f"   [제목] {title}\n"
                        f"   [시간] {pub_date}\n"
                        f"   [링크] {link}"
                    )
                
                news_text = "\n\n".join(news_items)
                total_count = data.get("total", len(data["items"]))
                ctx.info(f"Found {total_count} news items for '{keyword}', showing {len(data['items'])}")
                
                return f""""{keyword}"에 관한 뉴스 {total_count}건 중 {len(data["items"])}건을 찾았습니다.

{news_text}

이 뉴스들의 맥락과 주요 내용을 분석해주세요. 다음 사항에 대해 알려주세요:
1. 제목들의 공통 주제
2. 주요 키워드 3개
3. 어떤 사건이나 이슈를 다루고 있는지
4. 이 뉴스들이 시사하는 사회적/경제적/정치적 맥락
"""
            except httpx.TimeoutException:
                ctx.info("Request timed out after retries")
                return f"검색 중 시간 초과가 발생했습니다. 잠시 후 다시 시도해 주세요."
                
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                ctx.info(f"HTTP error {status_code}: {e.response.text[:100]}")
                
                if status_code == 401 or status_code == 403:
                    return "API 인증에 실패했습니다. API 키를 확인해 주세요."
                elif status_code == 429:
                    return "너무 많은 요청을 보냈습니다. 잠시 후 다시 시도해 주세요."
                elif status_code >= 500:
                    return "서버 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                else:
                    return f"검색 중 오류가 발생했습니다 (HTTP {status_code})"
                    
            except httpx.RequestError as e:
                ctx.info(f"Request error: {str(e)}")
                return "네트워크 연결 문제가 발생했습니다. 인터넷 연결을 확인해 주세요."
                
    except Exception as e:
        ctx.info(f"Unexpected error in search_news: {str(e)}")
        return f"뉴스 검색 중 예상치 못한 오류가 발생했습니다: {str(e)}"

@mcp.prompt()
def analyze_news_context_prompt(search_results: str) -> list:
    """뉴스 맥락 분석 프롬프트"""
    return [
        base.UserMessage(
            """다음 뉴스 기사들을 분석하여 맥락과 관계를 파악해주세요.

1. 공통 주제 및 메인 스토리: 이 뉴스들이 다루는 핵심 이슈나 사건은 무엇인가요?
2. 주요 키워드 5개: 이 뉴스들에서 반복적으로 등장하는 핵심 키워드는 무엇인가요?
3. 관점 분석: 각 언론사별로 보도 관점이나 강조점에 차이가 있나요?
4. 시간적 맥락: 이 이슈가 어떻게 발전해왔고, 향후 어떻게 전개될 가능성이 있나요?
5. 사회적/경제적/정치적 의미: 이 뉴스들이 어떤 더 넓은 맥락에서 중요한지 설명해주세요.

다음 뉴스 정보를 바탕으로 분석해주세요:"""
        ),
        base.UserMessage(search_results),
    ]

@mcp.tool()
async def search_news_with_content(keyword: str, ctx: Context) -> str:
    """
    키워드로 뉴스 검색 및 첫 번째 기사 내용 가져오기
    
    Args:
        keyword: 검색할 키워드
    
    Returns:
        검색된 뉴스 제목 및 첫 번째 기사 내용
    """
    if not keyword or keyword.strip() == "":
        return "검색어를 입력해주세요."
    
    try:
        ctx.info(f"Searching for news with content about: {keyword}")
        
        # 키워드 검증 및 인코딩 처리
        keyword = keyword.strip()
        
        # 네이버 API로 검색 수행
        search_url = "https://openapi.naver.com/v1/search/news.json"
        params = {
            "query": keyword,
            "display": 20,  # 최대 20개 결과만 가져옴 (5 -> 20으로 변경)
            "sort": "sim"  # 유사도순 정렬
        }
        
        async with httpx.AsyncClient() as client:
            try:
                # 재시도 로직이 포함된 요청 함수 사용
                response = await make_request_with_retry(
                    client, 
                    search_url, 
                    params=params, 
                    headers=NAVER_HEADERS
                )
                
                data = response.json()
                
                if not data.get("items"):
                    return f"'{keyword}'에 대한 검색 결과가 없습니다."
                
                # 뉴스 정보 추출 및 구조화 (각 기사의 내용까지 추출)
                news_items = []
                tasks = []
                
                # 비동기로 모든 기사 내용 추출 작업 생성
                for item in data["items"]:
                    link = item.get("link", "#")
                    tasks.append(extract_article_content(link, client))
                
                # 모든 비동기 작업 동시 실행
                contents = await asyncio.gather(*tasks)
                
                # 결과 조합
                for i, (item, content) in enumerate(zip(data["items"], contents)):
                    title = item.get("title", "제목 없음").replace("<b>", "").replace("</b>", "")
                    link = item.get("link", "#")
                    pub_date = format_date(item.get("pubDate", "날짜 정보 없음"))
                    publisher = extract_publisher(item)
                    
                    # 구조화된 형식으로 정보 추가 (본문 포함)
                    news_items.append(
                        f"{i+1}. [언론사] {publisher}\n"
                        f"   [제목] {title}\n"
                        f"   [시간] {pub_date}\n"
                        f"   [링크] {link}\n"
                        f"   [본문]\n{content}"
                    )
                
                news_text = "\n\n" + "\n\n".join(news_items)
                total_count = data.get("total", len(data["items"]))
                ctx.info(f"Found {total_count} news items with content for '{keyword}', showing {len(data['items'])}")
                
                return f""""{keyword}"에 관한 뉴스 {total_count}건 중 {len(data["items"])}건의 제목과 내용을 찾았습니다.

{news_text}

이 뉴스들의 상세 내용을 바탕으로 다음 사항을 분석해주세요:
1. 주요 사건/이슈 요약 (5-6문장)
2. 핵심 인물, 기관, 장소
3. 각 언론사별 보도 관점 차이
4. 사회적/경제적/정치적 맥락과 영향
5. 향후 전개 가능성
"""
            except Exception as e:
                ctx.info(f"Error in API request or processing: {str(e)}")
                return f"뉴스 검색 및 내용 가져오기 중 오류가 발생했습니다: {str(e)}"
                
    except Exception as e:
        ctx.info(f"Unexpected error in search_news_with_content: {str(e)}")
        return f"뉴스 검색 및 내용 가져오기 중 예상치 못한 오류가 발생했습니다: {str(e)}"

@mcp.tool()
async def compare_news_perspectives(keyword: str, ctx: Context) -> str:
    """
    키워드 관련 뉴스의 다양한 관점 비교 분석
    
    Args:
        keyword: 검색할 키워드
    
    Returns:
        다양한 언론사의 관점 비교 분석
    """
    try:
        ctx.info(f"Comparing news perspectives for: {keyword}")
        
        # 기본 뉴스 검색 수행
        news_result = await search_news_with_content(keyword, ctx)
        
        # 여기서는 search_news_with_content 결과를 그대로 반환하며,
        # 프롬프트에서 관점 비교 분석을 요청하는 부분을 추가
        
        return f"{news_result}\n\n특히 각 언론사별 보도 관점과 프레임의 차이점을 중점적으로 분석해 주세요."
    
    except Exception as e:
        ctx.info(f"Error in compare_news_perspectives: {str(e)}")
        return f"뉴스 관점 비교 중 오류가 발생했습니다: {str(e)}"

def check_api_keys():
    """API 키가 설정되었는지 확인"""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        logger.warning("Naver API 키가 설정되지 않았습니다. .env 파일을 확인하세요.")
        return False
    return True

def check_dependencies():
    """필요한 라이브러리가 설치되어 있는지 확인"""
    missing_libs = []
    
    try:
        import httpx
    except ImportError:
        missing_libs.append("httpx")
    
    try:
        import bs4
    except ImportError:
        missing_libs.append("beautifulsoup4")
    
    try:
        import dotenv
    except ImportError:
        missing_libs.append("python-dotenv")
    
    if missing_libs:
        logger.error(f"필수 라이브러리가 누락되었습니다: {', '.join(missing_libs)}")
        logger.error(f"다음 명령어로 설치해주세요: pip install {' '.join(missing_libs)}")
        return False
    
    return True

if __name__ == "__main__":
    try:
        logger.info("News Context Analyzer MCP 서버 시작 중...")
        
        # 의존성 확인
        if not check_dependencies():
            logger.error("필수 라이브러리가 누락되어 서버를 시작할 수 없습니다.")
            sys.exit(1)
        
        # API 키 확인
        if not check_api_keys():
            logger.warning("API 키가 누락되었습니다. 기본값으로 진행합니다.")
        
        logger.info("사용 가능한 도구:")
        logger.info("- simple_test: 간단한 테스트 도구")
        logger.info("- search_news: 뉴스 검색 (언론사, 제목, 시간, 링크 정보 제공)")
        logger.info("- search_news_with_content: 뉴스 검색 및 내용 가져오기")
        logger.info("- compare_news_perspectives: 다양한 언론사의 관점 비교 분석")
        
        # MCP 서버 실행
        logger.info("MCP 서버 시작... 연결 대기 중")
        mcp.run()
    except Exception as e:
        logger.error(f"서버 시작 중 오류 발생: {str(e)}")
        sys.exit(1)