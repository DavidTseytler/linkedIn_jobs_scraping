from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uuid, json, sqlite3, logging, requests, time, random, os, io
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import quote
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import pandas as pd
from contextlib import contextmanager
from dataclasses import dataclass
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


# Модели данных
@dataclass
class JobData:
    title: str
    company: str
    location: str
    job_link: str
    posted_date: str
    url: str


class ScraperConfig:
    BASE_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    JOBS_PER_PAGE = 10
    MIN_DELAY = 3
    MAX_DELAY = 7
    PROXY_TIMEOUT = 30
    MAX_RETRIES = 3
    RATE_LIMIT_DELAY = 30
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }


class LinkedInScraper:
    def __init__(self):
        self.session = self._init_session()
        self.proxies = self._init_proxies()
        self.current_proxy_idx = 0

    def _init_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(total=ScraperConfig.MAX_RETRIES, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _init_proxies(self) -> List[Dict[str, str]]:
        proxy_list = [
"jeCqBL:Ft5S0x@163.198.234.169:8000",
"jeCqBL:Ft5S0x@163.198.233.21:8000",
        ]
        return [{"http": f"http://{proxy}", "https": f"http://{proxy}"} for proxy in proxy_list]

    def _get_proxy(self) -> Dict[str, str]:
        proxy = self.proxies[self.current_proxy_idx]
        self.current_proxy_idx = (self.current_proxy_idx + 1) % len(self.proxies)
        return proxy

    def _fetch_page(self, url: str) -> Optional[BeautifulSoup]:
        for attempt in range(ScraperConfig.MAX_RETRIES):
            try:
                response = self.session.get(
                    url,
                    headers=ScraperConfig.HEADERS,
                    proxies=self._get_proxy(),
                    timeout=ScraperConfig.PROXY_TIMEOUT
                )
                if response.status_code == 429:
                    logger.info(f"Rate limit hit. Waiting {ScraperConfig.RATE_LIMIT_DELAY} sec...")
                    time.sleep(ScraperConfig.RATE_LIMIT_DELAY)
                    continue
                if response.status_code != 200:
                    raise Exception(f"HTTP Error {response.status_code}")
                return BeautifulSoup(response.text, "lxml")
            except Exception as e:
                logger.error(f"Error (attempt {attempt + 1}): {str(e)}")
                time.sleep(random.uniform(2, 4))
        return None

    def scrape_jobs(self, keyword: str, location: str, max_jobs: int = 1000) -> List[JobData]:
        jobs = []
        start = 0
        encoded_location = quote(location)
        seen_urls = set()  # Для отслеживания уже обработанных URL
        retry_counts = {}  # Для подсчета повторных запросов

        while len(jobs) < max_jobs and start < 1000:
            url = f"{ScraperConfig.BASE_URL}?keywords={quote(keyword)}&location={encoded_location}&start={start}"

            # Проверяем, не превысили ли лимит повторных запросов
            if url in retry_counts:
                retry_counts[url] += 1
                if retry_counts[url] > 3:
                    logger.warning(f"Too many retries for URL: {url}. Breaking the loop.")
                    break
            else:
                retry_counts[url] = 1

            logger.info(f"Fetching: {url} (attempt {retry_counts[url]})")

            if soup := self._fetch_page(url):
                if cards := soup.find_all("div", class_="base-card"):
                    new_jobs_count = 0
                    for card in cards:
                        try:
                            title = card.find("h3", class_="base-search-card__title").get_text(strip=True)
                            company = card.find("h4", class_="base-search-card__subtitle").get_text(strip=True)
                            location_text = card.find("span", class_="job-search-card__location").get_text(strip=True)
                            link = card.find("a", class_="base-card__full-link")["href"].split("?")[0]
                            date = card.find("time", class_="job-search-card__listdate")
                            if date:
                                date = date.get("datetime")

                            if link not in seen_urls:
                                seen_urls.add(link)
                                jobs.append(JobData(title, company, location_text, link, date, url))
                                new_jobs_count += 1
                                if len(jobs) >= max_jobs:
                                    break
                        except Exception as e:
                            logger.error(f"Error processing job: {str(e)}")

                    # Если не найдено новых вакансий, прерываем цикл
                    if new_jobs_count == 0:
                        logger.info("No new jobs found, ending scraping")
                        break

                    start += ScraperConfig.JOBS_PER_PAGE
                else:
                    logger.info("No job cards found, ending scraping")
                    break
            else:
                logger.error(f"Failed to fetch page for start={start}")

            time.sleep(random.uniform(ScraperConfig.MIN_DELAY, ScraperConfig.MAX_DELAY))

        return jobs[:max_jobs]


# FastAPI приложение
app = FastAPI(
    title="LinkedIn Jobs Scraper API",
    description="API для парсинга вакансий с LinkedIn с возможностью исключения компаний",
    version="2.0.0"
)
templates = Jinja2Templates(directory="templates")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


# Модели Pydantic
class ScrapeRequest(BaseModel):
    keywords: str
    location: str = ""
    max_jobs: int = 1000
    exclude_companies: List[str] = []


class ScrapeTask(BaseModel):
    id: str
    status: str
    created_at: str
    finished_at: Optional[str] = None
    result_count: Optional[int] = None
    keywords: Optional[str] = None
    location: Optional[str] = None
    max_jobs: Optional[int] = None
    exclude_companies: Optional[List[str]] = None


class JobResult(BaseModel):
    task_id: str
    created_at: str
    search_keywords: str
    search_location: str
    title: str
    company: str
    location: str
    job_link: str
    posted_date: str


# Инициализация БД
def init_db():
    with sqlite3.connect('scraper.db') as conn:
        # Создаем таблицу, если она не существует
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                status TEXT,
                created_at TEXT,
                finished_at TEXT,
                result_count INTEGER,
                result_json TEXT,
                keywords TEXT,
                location TEXT,
                max_jobs INTEGER,
                exclude_companies TEXT
            )""")

        # Проверяем существование столбца exclude_companies
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'exclude_companies' not in columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN exclude_companies TEXT")

        conn.commit()
    logger.info("Database initialized")


init_db()


@contextmanager
def get_db_connection():
    conn = sqlite3.connect('scraper.db')
    try:
        yield conn
    finally:
        conn.close()


def update_task_status(task_id: str, status: str):
    with get_db_connection() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        conn.commit()
    logger.info(f"Updated task {task_id} status to {status}")


def save_results(task_id: str, jobs: list, exclude_companies: List[str] = None):
    result_data = {
        "jobs": [j.__dict__ for j in jobs],
        "exclude_companies": exclude_companies or []
    }

    with get_db_connection() as conn:
        conn.execute(
            """UPDATE tasks SET 
            status=?, 
            finished_at=?, 
            result_count=?, 
            result_json=?,
            exclude_companies=?
            WHERE id=?""",
            (
                "completed",
                datetime.now().isoformat(),
                len(jobs),
                json.dumps(result_data),
                json.dumps(exclude_companies or []),
                task_id
            )
        )
        conn.commit()
    logger.info(f"Results saved for {task_id}")


# API Endpoints
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    if not os.path.exists("templates/index.html"):
        raise HTTPException(status_code=500, detail="index.html not found")

    # Получаем сохраненные исключенные компании из куки или query string
    exclude_companies = request.cookies.get("excluded_companies", "")
    query_exclude = request.query_params.get("exclude_companies", "")
    exclude_companies = query_exclude if query_exclude else exclude_companies

    exclude_list = [c.strip() for c in exclude_companies.split(",") if c.strip()]

    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "all_jobs": await get_all_job_results(exclude_list),
            "exclude_companies": exclude_companies
        }
    )

    # Устанавливаем куку с исключенными компаниями
    if exclude_companies:
        response.set_cookie(key="excluded_companies", value=exclude_companies)

    return response


async def get_all_job_results(exclude_companies: List[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, created_at, result_json, keywords, location, exclude_companies 
            FROM tasks WHERE (status = 'completed' OR status = 'partial') 
            AND result_json IS NOT NULL"""
        )
        tasks = cursor.fetchall()

    all_jobs = []
    seen_links = set()

    for task in tasks:
        task_id, created_at, result_json, search_keywords, search_location, task_exclude = task
        try:
            # Получаем исключенные компании для каждой задачи
            task_exclude_list = json.loads(task_exclude) if task_exclude else []
            combined_exclude = list(set((exclude_companies or []) + task_exclude_list))

            data = json.loads(result_json)
            jobs = data.get("jobs", [])

            for job in jobs:
                full_link = job.get('job_link')
                if not full_link:
                    continue

                if full_link in seen_links:
                    continue
                seen_links.add(full_link)

                # Фильтрация по исключенным компаниям
                if combined_exclude and job.get('company', '').lower() in [
                    c.lower() for c in combined_exclude
                ]:
                    continue

                job['task_id'] = task_id
                job['created_at'] = created_at
                job['search_keywords'] = search_keywords
                job['search_location'] = search_location
                all_jobs.append(job)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in task {task_id}")

    return all_jobs


@app.post("/api/scrape", response_model=ScrapeTask)
async def create_scrape_task(request: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat()

    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO tasks 
            (id, status, created_at, keywords, location, max_jobs, exclude_companies) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                "pending",
                created_at,
                request.keywords,
                request.location,
                request.max_jobs,
                json.dumps(request.exclude_companies)
            )
        )
        conn.commit()

    logger.info(f"Task created: {task_id}")
    background_tasks.add_task(run_scraper_task, task_id, request)

    return {
        "id": task_id,
        "status": "pending",
        "created_at": created_at,
        **request.dict()
    }


async def run_scraper_task(task_id: str, request: ScrapeRequest):
    try:
        logger.info(f"Starting scraping task {task_id} for {request.keywords} in {request.location}")
        update_task_status(task_id, "in_progress")

        scraper = LinkedInScraper()
        jobs = scraper.scrape_jobs(request.keywords, request.location, request.max_jobs)

        # Фильтрация по исключенным компаниям
        if request.exclude_companies:
            jobs = [
                job for job in jobs
                if job.company.lower() not in [
                    c.lower() for c in request.exclude_companies
                ]
            ]

        save_results(task_id, jobs, request.exclude_companies)
        logger.info(f"Finished scraping task {task_id}, found {len(jobs)} jobs (after excluding companies)")

    except Exception as e:
        logger.error(f"Error in task {task_id}: {str(e)}", exc_info=True)
        update_task_status(task_id, "failed")
        raise


@app.get("/api/tasks", response_model=List[ScrapeTask])
async def get_all_tasks():
    with get_db_connection() as conn:
        return [
            {
                "id": row[0],
                "status": row[1],
                "created_at": row[2],
                "finished_at": row[3],
                "result_count": row[4],
                "keywords": row[5],
                "location": row[6],
                "max_jobs": row[7],
                "exclude_companies": json.loads(row[8]) if row[8] else []
            }
            for row in conn.execute("""
                SELECT id, status, created_at, finished_at, result_count, 
                       keywords, location, max_jobs, exclude_companies 
                FROM tasks ORDER BY created_at DESC
            """)
        ]


@app.get("/api/tasks/{task_id}", response_model=ScrapeTask)
async def get_task_status(task_id: str):
    with get_db_connection() as conn:
        if task := conn.execute(
                """SELECT id, status, created_at, finished_at, result_count, 
                          keywords, location, max_jobs, exclude_companies 
                   FROM tasks WHERE id=?""",
                (task_id,)
        ).fetchone():
            return {
                "id": task[0],
                "status": task[1],
                "created_at": task[2],
                "finished_at": task[3],
                "result_count": task[4],
                "keywords": task[5],
                "location": task[6],
                "max_jobs": task[7],
                "exclude_companies": json.loads(task[8]) if task[8] else []
            }

    raise HTTPException(status_code=404, detail="Task not found")


@app.get("/api/all-results-filtered")
async def get_all_results_filtered(exclude_companies: str = ""):
    """Получает все результаты, исключая указанные компании (через запятую)"""
    exclude_list = [c.strip() for c in exclude_companies.split(",") if c.strip()]
    jobs = await get_all_job_results(exclude_list)
    return JSONResponse(content=jobs)


@app.get("/api/tasks/{task_id}/results")
async def get_task_results(task_id: str):
    with get_db_connection() as conn:
        if result := conn.execute(
                """SELECT result_json, keywords, location 
                   FROM tasks 
                   WHERE id=? AND (status='completed' OR status='partial')""",
                (task_id,)
        ).fetchone():
            try:
                data = json.loads(result[0])
                jobs = data.get("jobs", [])

                return JSONResponse(
                    content=[
                        {
                            **job,
                            'task_id': task_id,
                            'search_keywords': result[1],
                            'search_location': result[2]
                        }
                        for job in jobs
                    ]
                )
            except json.JSONDecodeError:
                raise HTTPException(status_code=500, detail="Invalid results data")

    raise HTTPException(status_code=404, detail="Results not available")


@app.get("/api/all-results")
async def get_all_results():
    return JSONResponse(content=await get_all_job_results())


@app.get("/api/export-excel")
async def export_to_excel(exclude_companies: str = ""):
    exclude_list = [c.strip() for c in exclude_companies.split(",") if c.strip()]
    jobs = await get_all_job_results(exclude_list)

    if not jobs:
        raise HTTPException(status_code=404, detail="No jobs found to export")

    df = pd.DataFrame(jobs)[[
        'task_id', 'created_at', 'search_keywords', 'search_location',
        'title', 'company', 'location', 'job_link', 'posted_date'
    ]]
    df.columns = [
        'Task ID', 'Task Created', 'Search Keywords', 'Search Location',
        'Job Title', 'Company', 'Job Location', 'Job Link', 'Posted Date'
    ]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, sheet_name='LinkedIn Jobs', index=False)
        worksheet = writer.sheets['LinkedIn Jobs']

        # Форматирование
        url_format = writer.book.add_format({'color': 'blue', 'underline': 1})
        text_format = writer.book.add_format({'num_format': '@'})

        worksheet.set_column('A:A', 36)
        worksheet.set_column('B:B', 20)
        worksheet.set_column('C:C', 30)
        worksheet.set_column('D:D', 25)
        worksheet.set_column('E:E', 30)
        worksheet.set_column('F:F', 25)
        worksheet.set_column('G:G', 25)
        worksheet.set_column('H:H', 100, text_format)
        worksheet.set_column('I:I', 15)

        # Добавляем гиперссылки
        for row_num, url in enumerate(df['Job Link'], start=1):
            if pd.notna(url):
                worksheet.write_url(
                    f'H{row_num + 1}',
                    url,
                    string=url,
                    cell_format=url_format
                )

    output.seek(0)
    return StreamingResponse(
        output,
        headers={
            'Content-Disposition': f'attachment; filename="linkedin_jobs_export.xlsx"',
            'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        }
    )


@app.delete("/api/jobs/delete")
async def delete_jobs(job_links: List[str]):
    with get_db_connection() as conn:
        deleted_count = 0
        for task_id, result_json in conn.execute(
                """SELECT id, result_json 
                   FROM tasks 
                   WHERE (status='completed' OR status='partial') 
                   AND result_json IS NOT NULL"""
        ):
            try:
                data = json.loads(result_json)
                jobs = data.get("jobs", [])
                updated_jobs = [
                    j for j in jobs
                    if j.get('job_link') not in job_links
                       and j.get('link') not in job_links
                       and j.get('url') not in job_links
                ]

                if len(updated_jobs) < len(jobs):
                    deleted_count += len(jobs) - len(updated_jobs)
                    data["jobs"] = updated_jobs
                    conn.execute(
                        """UPDATE tasks 
                        SET result_json=?, result_count=? 
                        WHERE id=?""",
                        (json.dumps(data), len(updated_jobs), task_id))
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in task {task_id}")

        conn.commit()
        return {
            "message": f"Deleted {deleted_count} jobs",
            "deleted_count": deleted_count
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
