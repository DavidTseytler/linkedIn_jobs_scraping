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
import pycountry
from urllib3.exceptions import InsecureRequestWarning

# Отключаем предупреждения о небезопасных SSL-запросах
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Конфигурация
DEFAULT_LIMIT = 1000
MAX_PER_PAGE = 100
PROXY_LIST = ["4vJkDS:vAELf6@163.198.235.139:8000", "4vJkDS:vAELf6@163.198.235.4:8000"]
REQUEST_DELAY = 2

# GraphQL запрос для Indeed (из рабочего кода)
JOB_SEARCH_QUERY = """
query GetJobData {{
    jobSearch(
    {what}
    {location}
    limit: {limit}
    {cursor}
    sort: RELEVANCE
    {filters}
    ) {{
    pageInfo {{
        nextCursor
    }}
    results {{
        trackingKey
        job {{
        key
        title
        datePublished
        dateOnIndeed
        description {{
            html
            text
        }}
        location {{
            countryName
            countryCode
            admin1Code
            city
            postalCode
            streetAddress
            formatted {{
            short
            long
            }}
        }}
        compensation {{
            estimated {{
            currencyCode
            baseSalary {{
                unitOfWork
                range {{
                ... on Range {{
                    min
                    max
                }}
                }}
            }}
            }}
            baseSalary {{
            unitOfWork
            range {{
                ... on Range {{
                min
                max
                }}
            }}
            }}
            currencyCode
        }}
        attributes {{
            key
            label
        }}
        employer {{
            relativeCompanyPageUrl
            name
            dossier {{
                employerDetails {{
                addresses
                industry
                employeesLocalizedLabel
                revenueLocalizedLabel
                briefDescription
                ceoName
                ceoPhotoUrl
                }}
                images {{
                    headerImageUrl
                    squareLogoUrl
                }}
                links {{
                corporateWebsite
            }}
            }}
        }}
        recruit {{
            viewJobUrl
            detailedSalary
            workSchedule
        }}
        }}
    }}
    }}
}}
"""


# Модели данных - расширенная версия для поддержки всех полей из рабочего кода
@dataclass
class JobData:
    title: str
    company: str
    location: str
    job_link: str
    posted_date: str
    platform: str
    search_term: str = ""
    search_location: str = ""
    country_name: str = ""
    country_code: str = ""
    work_attributes: str = ""
    description_text: str = ""
    description_html: str = ""
    url: str = ""
    task_id: str = ""
    created_at: str = ""


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


def get_country_config(country_input):
    """
    Автоматически генерирует конфигурацию для страны с помощью pycountry (улучшенная версия)
    """
    print(f"🌍 Generating config for country input: '{country_input}'")

    try:
        # Нормализуем ввод - убираем пробелы и приводим к верхнему регистру
        country_input = country_input.strip().upper()

        # Специальные преобразования (как в рабочем скрипте)
        if country_input == "UK":
            country_input = "GB"
            print("🔄 Converted UK to GB")
        elif country_input == "USA" or country_input == "UNITED STATES":
            country_input = "US"
            print("🔄 Converted to US code")

        # Пытаемся найти страну по разным критериям
        country = pycountry.countries.search_fuzzy(country_input)[0]
        country_code = country.alpha_2
        country_name = country.name

        print(f"✅ Country identified: {country_name} ({country_code})")

        # Особые случаи для domain (как в рабочем скрипте)
        special_domains = {
            "US": "www.indeed.com",
            "GB": "uk.indeed.com",  # Великобритания
        }

        domain = special_domains.get(country_code)
        if not domain:
            # Для остальных стран: код.lower().indeed.com
            domain = f"{country_code.lower()}.indeed.com"

        # Особые случаи для co (кода страны)
        special_co = {
            "GB": "GB",  # Великобритания
        }
        co = special_co.get(country_code, country_code)

        # Формируем locale и accept-language
        locale = f"en-{co}"
        accept_language = f"{locale},en;q=0.9"

        config = {
            "domain": domain,
            "locale": locale,
            "co": co,
            "accept_language": accept_language,
            "country_name": country_name,
            "country_code": country_code
        }

        print(f"✅ Config generated for {country_name}:")
        print(f"   - Domain: {domain}")
        print(f"   - Locale: {locale}")
        print(f"   - Country Code: {co}")
        print(f"   - Accept-Language: {accept_language}")

        return config

    except (LookupError, IndexError) as e:
        print(f"❌ Country '{country_input}' not found, using default US config")
        # Если страна не найдена, используем конфигурацию по умолчанию для US
        return {
            "domain": "www.indeed.com",
            "locale": "en-US",
            "co": "US",
            "accept_language": "en-US,en;q=0.9",
            "country_name": "United States",
            "country_code": "US"
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
        proxy_list = PROXY_LIST
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
        seen_urls = set()
        retry_counts = {}

        while len(jobs) < max_jobs and start < 1000:
            url = f"{ScraperConfig.BASE_URL}?keywords={quote(keyword)}&location={encoded_location}&start={start}"

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
                                job = JobData(
                                    title=title,
                                    company=company,
                                    location=location_text,
                                    job_link=link,
                                    posted_date=date or "",
                                    platform="linkedin",
                                    search_term=keyword,
                                    search_location=location
                                )
                                jobs.append(job)
                                new_jobs_count += 1
                                if len(jobs) >= max_jobs:
                                    break
                        except Exception as e:
                            logger.error(f"Error processing job: {str(e)}")

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


class IndeedUniversalScraper:
    def __init__(self, country_code=""):
        print(f"🚀 Initializing Indeed scraper for country: '{country_code}'")

        # Сохраняем оригинальный ввод для логирования
        self.original_country_input = country_code

        # Получаем конфиг на основе введенной страны
        self.config = get_country_config(country_code)
        self.country_code = self.config["country_code"]  # Используем нормализованный код из конфига

        print(f"✅ Indeed Scraper configured for: {self.config['country_name']} ({self.country_code})")

        self.api_url = "https://apis.indeed.com/graphql"
        self.base_url = f"https://{self.config['domain']}"
        self.headers = self._prepare_headers()
        self.proxies = self._prepare_proxies()
        self.session = requests.Session()
        self.request_delay = REQUEST_DELAY
        self.max_per_page = MAX_PER_PAGE
        self.default_limit = DEFAULT_LIMIT

        logger.info(f"Indeed Scraper initialized for country: {self.config['country_name']} ({self.country_code})")
        logger.info(f"API URL: {self.api_url}")
        logger.info(f"Base URL: {self.base_url}")

    def _prepare_headers(self):
        headers = {
            "Host": "apis.indeed.com",
            "content-type": "application/json",
            "indeed-api-key": "161092c2017b5bbab13edb12461a62d5a833871e7cad6d9d475304573de67ac8",
            "accept": "application/json",
            "indeed-locale": self.config["locale"],
            "accept-language": self.config["accept_language"],
            "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Indeed App 193.1",
            "indeed-app-info": "appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone",
            "indeed-co": self.config["co"],
        }
        print(f"📋 Headers configured with:")
        print(f"   - Indeed-Locale: {headers['indeed-locale']}")
        print(f"   - Indeed-Co: {headers['indeed-co']}")
        print(f"   - Accept-Language: {headers['accept-language']}")
        return headers

    def _prepare_proxies(self) -> List[Dict]:
        proxies = []
        for proxy in PROXY_LIST:
            if "@" in proxy:
                auth, host = proxy.split("@")
                proxy_url = f"http://{auth}@{host}"
                proxies.append({
                    "http": proxy_url,
                    "https": proxy_url
                })
        print(f"🔌 Proxies configured: {len(proxies)} proxies")
        return proxies

    def _build_query(self, search_term: str, location: str, cursor: Optional[str], limit: int,
                     hours_old: int = None) -> str:
        print(f"🔨 Building query for: '{search_term}' in '{location}', limit: {limit}, hours_old: {hours_old}")

        what = f'what: "{search_term}"' if search_term else ""
        location_param = f'location: {{where: "{location}", radius: 50, radiusUnit: MILES}}' if location else ""
        cursor_part = f'cursor: "{cursor}"' if cursor else ""

        # Добавляем фильтр по времени публикации
        filters = []
        if hours_old:
            filters.append(f"""
            date: {{
                field: "dateOnIndeed",
                start: "{hours_old}h"
            }}
            """)

        filters_str = f"filters: {{{','.join(filters)}}}" if filters else ""

        query = JOB_SEARCH_QUERY.format(
            what=what,
            location=location_param,
            limit=limit,
            cursor=cursor_part,
            filters=filters_str
        )

        print(f"📝 GraphQL query built successfully")
        return query

    def _make_request(self, query: str, attempt: int, max_attempts: int) -> Optional[Dict]:
        try:
            proxy = random.choice(self.proxies) if self.proxies else None
            print(f"🌐 Making request (attempt {attempt + 1}/{max_attempts}) to: {self.api_url}")
            if proxy:
                print(f"🔄 Using proxy: {proxy['http'].split('@')[1]}")

            response = self.session.post(
                self.api_url,
                headers=self.headers,
                json={"query": query},
                proxies=proxy,
                timeout=30,
                verify=False
            )

            print(f"📡 Response status: {response.status_code}")

            if response.status_code != 200:
                print(f"❌ Error response: {response.status_code}")
                print(f"📄 Response text: {response.text[:500]}...")
                if attempt < max_attempts - 1:
                    print(f"⏳ Retrying after delay...")
                    time.sleep(self.request_delay * 2)
                    return self._make_request(query, attempt + 1, max_attempts)
                return None

            print(f"✅ Request successful")
            return response.json()

        except Exception as e:
            print(f"💥 Request error: {e}")
            if attempt < max_attempts - 1:
                print(f"⏳ Retrying after delay...")
                time.sleep(self.request_delay * 2)
                return self._make_request(query, attempt + 1, max_attempts)
            return None

    def _parse_response(self, response: Dict, search_term: str, search_location: str) -> tuple:
        print(f"📊 Parsing API response...")

        if not response or "data" not in response:
            print(f"❌ No data in response or invalid JSON")
            if response:
                print(f"📄 Response: {response}")
            return [], None

        data = response["data"].get("jobSearch", {})
        results = data.get("results", [])
        next_cursor = data.get("pageInfo", {}).get("nextCursor")

        print(f"📈 Found {len(results)} results in API response")

        jobs = []
        for i, result in enumerate(results):
            job_data = result.get("job", {})
            if not job_data:
                continue

            # Обработка локации (из рабочего кода)
            location_data = job_data.get("location", {})
            country_name = location_data.get("countryName", "")
            country_code = location_data.get("countryCode", "")

            # Форматируем полную локацию (как в рабочем коде)
            job_location = location_data.get("formatted", {}).get("long", "")
            if not job_location:
                job_location_parts = [
                    location_data.get("city", ""),
                    location_data.get("admin1Code", ""),
                    country_name
                ]
                job_location = ", ".join(filter(None, job_location_parts))

            # Обработка даты
            timestamp = job_data.get("datePublished")
            date_posted = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M:%S") if timestamp else None

            # Обработка работодателя
            employer_data = job_data.get("employer")
            company_name = employer_data.get("name") if employer_data else "Not specified"

            # Обработка описания (из рабочего кода)
            description = job_data.get("description", {})
            description_text = description.get("text", "")
            description_html = description.get("html", "")

            # Атрибуты работы (из рабочего кода)
            attributes = job_data.get("attributes", [])
            work_attributes = [attr.get("label", "") for attr in attributes]

            job = JobData(
                title=job_data.get("title", ""),
                company=company_name,
                location=job_location,
                job_link=f'{self.base_url}/viewjob?jk={job_data["key"]}',
                posted_date=date_posted or "",
                platform="indeed",
                search_term=search_term,
                search_location=search_location,
                country_name=country_name,
                country_code=country_code,
                work_attributes=", ".join(work_attributes),
                description_text=description_text[:1000] + "..." if len(description_text) > 1000 else description_text,
                description_html=description_html[:500] + "..." if len(description_html) > 500 else description_html
            )
            jobs.append(job)
            print(f"✅ Job {i + 1}: {job.title} at {job.company}")

        print(f"📦 Parsed {len(jobs)} jobs total")
        return jobs, next_cursor

    def scrape_jobs(self, search_term: str, location: str = "", limit: int = DEFAULT_LIMIT,
                    hours_old: int = None) -> List[JobData]:
        print(f"\n🎯 STARTING INDEED SCRAPE")
        print(f"📍 Country: {self.config['country_name']} ({self.country_code})")
        print(f"🔍 Search: '{search_term}'")
        print(f"🗺️ Location: '{location if location else 'Not specified'}'")
        print(f"📊 Limit: {limit} jobs")
        print(f"⏰ Hours old: {hours_old if hours_old else 'Not specified'}")
        print("=" * 60)

        # Если локация не указана, используем название страны как локацию
        if not location:
            location = self.config['country_name']
            print(f"🔄 Using country name as location: {location}")

        jobs = []
        cursor = None
        max_attempts = 3
        page = 0

        while len(jobs) < limit:
            page += 1
            print(f"\n📄 Page {page} - Current total: {len(jobs)}/{limit} jobs")

            # Строим запрос
            current_limit = min(self.max_per_page, limit - len(jobs))
            query = self._build_query(search_term, location, cursor, current_limit, hours_old)

            # Делаем запрос
            response = self._make_request(query, 0, max_attempts)

            if not response:
                print(f"❌ Failed to get response from Indeed API")
                break

            # Парсим ответ
            new_jobs, cursor = self._parse_response(response, search_term, location)
            jobs.extend(new_jobs)

            print(f"✅ Page {page} completed: {len(new_jobs)} new jobs")

            # Проверяем условия завершения
            if not cursor:
                print(f"🏁 No more pages available")
                break

            if len(jobs) >= limit:
                print(f"🏁 Limit reached: {len(jobs)} jobs")
                break

            # Задержка между запросами
            print(f"⏳ Waiting {self.request_delay}s before next page...")
            time.sleep(self.request_delay)

        print(f"\n🎉 SCRAPING COMPLETED!")
        print(f"📈 Total jobs found: {len(jobs)}")
        print(f"🌍 Country: {self.config['country_name']}")
        print(f"🔍 Search term: '{search_term}'")
        print("=" * 60)

        return jobs[:limit]


# FastAPI приложение
app = FastAPI(
    title="LinkedIn & Indeed Jobs Scraper API",
    description="API для парсинга вакансий с LinkedIn и Indeed с поддержкой множества стран",
    version="2.1.0"
)

# Создаем директорию для шаблонов если ее нет
os.makedirs("templates", exist_ok=True)

# Создаем базовый HTML шаблон с нужными колонками

        
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
    platform: str = "linkedin"
    country_code: str = ""
    hours_old: Optional[int] = None


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
    platform: Optional[str] = None
    country_code: Optional[str] = None
    hours_old: Optional[int] = None


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
    platform: str
    country_name: Optional[str] = ""
    country_code: Optional[str] = ""
    work_attributes: Optional[str] = ""


# Инициализация БД
def init_db():
    with sqlite3.connect('scraper.db') as conn:
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
                exclude_companies TEXT,
                platform TEXT,
                country_code TEXT,
                hours_old INTEGER
            )""")
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


def save_results(task_id: str, jobs: list, exclude_companies: List[str] = None, platform: str = "linkedin",
                 country_code: str = "", hours_old: int = None):
    # Добавляем task_id и created_at к каждой job
    for job in jobs:
        job.task_id = task_id
        job.created_at = datetime.now().isoformat()

    result_data = {
        "jobs": [j.__dict__ for j in jobs],
        "exclude_companies": exclude_companies or [],
        "platform": platform,
        "country_code": country_code,
        "hours_old": hours_old
    }

    with get_db_connection() as conn:
        conn.execute(
            """UPDATE tasks SET 
            status=?, 
            finished_at=?, 
            result_count=?, 
            result_json=?,
            exclude_companies=?,
            platform=?,
            country_code=?,
            hours_old=?
            WHERE id=?""",
            (
                "completed",
                datetime.now().isoformat(),
                len(jobs),
                json.dumps(result_data),
                json.dumps(exclude_companies or []),
                platform,
                country_code,
                hours_old,
                task_id
            )
        )
        conn.commit()
    logger.info(f"Results saved for {task_id}")


# API Endpoints
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
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

    if exclude_companies:
        response.set_cookie(key="excluded_companies", value=exclude_companies)

    return response


async def get_all_job_results(exclude_companies: List[str] = None) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, created_at, result_json, keywords, location, exclude_companies, platform, country_code, hours_old 
            FROM tasks WHERE (status = 'completed' OR status = 'partial') 
            AND result_json IS NOT NULL"""
        )
        tasks = cursor.fetchall()

    all_jobs = []
    seen_links = set()

    for task in tasks:
        task_id, created_at, result_json, search_keywords, search_location, task_exclude, platform, country_code, hours_old = task
        try:
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

                if combined_exclude and job.get('company', '').lower() in [
                    c.lower() for c in combined_exclude
                ]:
                    continue

                # Ensure all required fields are present
                job['task_id'] = job.get('task_id', task_id)
                job['created_at'] = job.get('created_at', created_at)
                job['search_keywords'] = job.get('search_keywords', search_keywords)
                job['search_location'] = job.get('search_location', search_location)
                job['platform'] = platform or data.get('platform', 'linkedin')
                job['country_code'] = country_code or data.get('country_code', '')
                job['hours_old'] = hours_old or data.get('hours_old')

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
            (id, status, created_at, keywords, location, max_jobs, exclude_companies, platform, country_code, hours_old) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                "pending",
                created_at,
                request.keywords,
                request.location,
                request.max_jobs,
                json.dumps(request.exclude_companies),
                request.platform,
                request.country_code,
                request.hours_old
            )
        )
        conn.commit()

    logger.info(f"Task created: {task_id} for platform: {request.platform}, country: {request.country_code}")
    background_tasks.add_task(run_scraper_task, task_id, request)

    return {
        "id": task_id,
        "status": "pending",
        "created_at": created_at,
        **request.dict()
    }


async def run_scraper_task(task_id: str, request: ScrapeRequest):
    try:
        logger.info(
            f"Starting scraping task {task_id} for '{request.keywords}' in '{request.location}' on {request.platform}, country: {request.country_code}")
        update_task_status(task_id, "in_progress")

        # АВТОМАТИЧЕСКОЕ ОПРЕДЕЛЕНИЕ СТРАНЫ ДЛЯ INDEED
        country_code = request.country_code

        if request.platform == "linkedin":
            scraper = LinkedInScraper()
            jobs = scraper.scrape_jobs(request.keywords, request.location, request.max_jobs)

        elif request.platform == "indeed":
            # Если country_code не указан, пытаемся определить из локации
            if not country_code and request.location:
                try:
                    print(f"🔍 Attempting to detect country from location: '{request.location}'")
                    country = pycountry.countries.search_fuzzy(request.location)[0]
                    country_code = country.alpha_2
                    print(f"✅ Auto-detected country: {country.name} ({country_code})")
                except Exception as e:
                    print(f"❌ Could not auto-detect country from '{request.location}': {e}")
                    # Используем локацию как есть (может быть городом или регионом)
                    # Для Indeed используем домен по умолчанию (US), но ищем в указанной локации
                    country_code = "US"
                    print(f"🔄 Using default country: {country_code}")

            # Если country_code все еще не определен, используем US
            if not country_code:
                country_code = "US"

            print(f"🎯 Final country code for Indeed: {country_code}")

            # Используем локацию из запроса (может быть страной, городом или регионом)
            location = request.location if request.location else country_code

            scraper = IndeedUniversalScraper(country_code=country_code)
            jobs = scraper.scrape_jobs(
                search_term=request.keywords,
                location=location,
                limit=request.max_jobs,
                hours_old=request.hours_old
            )
        else:
            raise ValueError(f"Unsupported platform: {request.platform}")

        if request.exclude_companies:
            jobs = [
                job for job in jobs
                if job.company.lower() not in [
                    c.lower() for c in request.exclude_companies
                ]
            ]

        # Сохраняем с определенным country_code
        save_results(task_id, jobs, request.exclude_companies, request.platform, country_code,
                     request.hours_old)
        logger.info(f"Finished scraping task {task_id}, found {len(jobs)} jobs (after excluding companies)")

    except Exception as e:
        logger.error(f"Error in task {task_id}: {str(e)}", exc_info=True)
        update_task_status(task_id, "failed")
        raise


@app.get("/api/jobs")
async def get_jobs(exclude_companies: str = ""):
    """API endpoint to get all jobs"""
    exclude_list = [c.strip() for c in exclude_companies.split(",") if c.strip()]
    jobs = await get_all_job_results(exclude_list)

    # Filter to only include required columns
    filtered_jobs = []
    for job in jobs:
        filtered_jobs.append({
            "platform": job.get("platform", ""),
            "task_id": job.get("task_id", ""),
            "created_at": job.get("created_at", ""),
            "search_keywords": job.get("search_keywords", ""),
            "search_location": job.get("search_location", ""),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "job_link": job.get("job_link", ""),
            "posted_date": job.get("posted_date", "")
        })

    return filtered_jobs


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Get task status by ID"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "id": task[0],
        "status": task[1],
        "created_at": task[2],
        "finished_at": task[3],
        "result_count": task[4],
        "keywords": task[6],
        "location": task[7],
        "max_jobs": task[8],
        "platform": task[10],
        "country_code": task[11],
        "hours_old": task[12]
    }


@app.get("/api/export/excel")
async def export_to_excel():
    """Export jobs to Excel"""
    jobs = await get_all_job_results()

    if not jobs:
        raise HTTPException(status_code=404, detail="No jobs found")

    # Create DataFrame with required columns
    df_data = []
    for job in jobs:
        df_data.append({
            "Platform": job.get("platform", ""),
            "Task ID": job.get("task_id", ""),
            "Created": job.get("created_at", ""),
            "Search Keywords": job.get("search_keywords", ""),
            "Search Location": job.get("search_location", ""),
            "Title": job.get("title", ""),
            "Company": job.get("company", ""),
            "Job Location": job.get("location", ""),
            "Job Link": job.get("job_link", ""),
            "Posted Date": job.get("posted_date", "")
        })

    df = pd.DataFrame(df_data)

    # Create Excel file in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Jobs', index=False)

    output.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"jobs_export_{timestamp}.xlsx"

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/api/export/csv")
async def export_to_csv():
    """Export jobs to CSV"""
    jobs = await get_all_job_results()

    if not jobs:
        raise HTTPException(status_code=404, detail="No jobs found")

    # Create CSV in memory
    output = io.StringIO()

    # Write header
    fieldnames = [
        "Platform", "Task ID", "Created", "Search Keywords", "Search Location",
        "Title", "Company", "Job Location", "Job Link", "Posted Date"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    # Write data
    for job in jobs:
        writer.writerow({
            "Platform": job.get("platform", ""),
            "Task ID": job.get("task_id", ""),
            "Created": job.get("created_at", ""),
            "Search Keywords": job.get("search_keywords", ""),
            "Search Location": job.get("search_location", ""),
            "Title": job.get("title", ""),
            "Company": job.get("company", ""),
            "Job Location": job.get("location", ""),
            "Job Link": job.get("job_link", ""),
            "Posted Date": job.get("posted_date", "")
        })

    output.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"jobs_export_{timestamp}.csv"

    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8')),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/api/test-indeed")
async def test_indeed(
        country_code: str = "US",
        keyword: str = "software engineer",
        location: str = "",
        limit: int = 10
):
    """Тестовый эндпоинт для проверки Indeed парсера"""
    try:
        print(f"\n🧪 TEST INDEED ENDPOINT")
        print(f"🌍 Country input: '{country_code}'")
        print(f"🔍 Keyword: '{keyword}'")
        print(f"📍 Location input: '{location}'")

        scraper = IndeedUniversalScraper(country_code=country_code)
        jobs = scraper.scrape_jobs(keyword, location, limit)

        return {
            "success": True,
            "original_country_input": country_code,
            "resolved_country": {
                "name": scraper.config["country_name"],
                "code": scraper.config["country_code"]
            },
            "keyword": keyword,
            "location": location if location else scraper.config["country_name"],
            "jobs_found": len(jobs),
            "jobs": [job.__dict__ for job in jobs],
            "config_used": scraper.config,
            "message": f"Found {len(jobs)} jobs for '{keyword}' in {scraper.config['country_name']}"
        }
    except Exception as e:
        logger.error(f"Test Indeed error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "original_country_input": country_code,
            "keyword": keyword,
            "location": location
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
