# Python 베이스 이미지 선택 (pyproject.toml의 requires-python = ">=3.10"에 맞춰서)
FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 필요한 시스템 패키지 설치 (선택적, lxml 등 C 의존성 라이브러리가 있다면 필요)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc

# 의존성 설치
# pyproject.toml에 dependencies가 정의되어 있다면, 아래 pip install . 로 충분할 수 있습니다.
# 현재는 main.py에서 확인된 라이브러리들을 직접 설치합니다.
COPY pyproject.toml ./
# RUN pip install uv # uv를 사용하여 pyproject.toml에서 빌드/설치하는 경우
# RUN uv pip install . # pyproject.toml에 의존성이 모두 명시된 경우

# main.py에서 확인된 라이브러리들 직접 설치
# mcp 라이브러리의 정확한 pip 패키지 이름을 확인해야 합니다. (예: fastmcp)
# 아래는 예시이며, 실제 fastmcp 패키지 이름으로 변경해야 합니다.
RUN pip install --no-cache-dir "fastmcp" httpx beautifulsoup4 python-dotenv gunicorn

# 애플리케이션 코드 복사
COPY ./ ./

# Naver API 키를 환경 변수로 전달받을 수 있도록 설정 (선택 사항, .env 파일 사용 권장)
# ENV NAVER_CLIENT_ID YOUR_NAVER_CLIENT_ID
# ENV NAVER_CLIENT_SECRET YOUR_NAVER_CLIENT_SECRET

# 애플리케이션 실행 포트 (FastMCP 기본 포트가 있다면 해당 포트로 변경)
# 일반적으로 웹 애플리케이션은 8080 포트를 많이 사용합니다.
EXPOSE 8080

# 애플리케이션 실행
# FastMCP가 gunicorn과 호환되는 ASGI/WSGI 앱을 제공하는지 확인 필요
# 그렇지 않다면 python main.py로 실행
# CMD ["python", "main.py"]

# gunicorn을 사용하여 프로덕션 환경에서 실행하는 것을 권장 (프로세스 관리, 워커 수 조절 등)
# main.py에서 mcp = FastMCP(...) 이므로, mcp 객체가 ASGI/WSGI 인터페이스를 제공해야 합니다.
# FastMCP가 ASGI를 지원한다면: CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080", "main:mcp"]
# FastMCP가 WSGI를 지원한다면: CMD ["gunicorn", "-b", "0.0.0.0:8080", "main:mcp"]
# 만약 위 방식이 작동하지 않으면, python main.py로 실행해야 합니다.
# 우선 python main.py 로 설정하고, 필요시 gunicorn 설정으로 변경하세요.
CMD ["python", "main.py"] 