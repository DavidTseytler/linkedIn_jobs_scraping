from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import uuid
import json
import sqlite3
from datetime import datetime
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote
import time
import random
from fake_useragent import UserAgent
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import os
import io
import pandas as pd

PROXIES = [
        "Cn0Am4:Mhq30q@45.145.58.212:8000",
        "HQTAwK:DejHvt@213.232.68.235:8000",

]
PERIOD_OPTIONS = {
    "24h": "f_TPR=r86400",        # Last 24 hours
    "7d": "f_TPR=r604800",        # Last 7 days
    "1m": "f_TPR=r2592000",       # Last month (~30 days)
    "any": "",              # Any time
}

app = FastAPI(title="LinkedIn Jobs Scraper API")

# Настройка шаблонов
templates = Jinja2Templates(directory="templates")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScrapeRequest(BaseModel):
    keywords: str
    location: str = ""
    period: str = ""
    max_pages: int = 10  # Максимальное количество страниц (10 jobs per page)


class ScrapeTask(BaseModel):
    id: str
    status: str
    created_at: str
    finished_at: Optional[str] = None
    result_count: Optional[int] = None
    keywords: Optional[str] = None
    location: Optional[str] = None
    period: Optional[str] = None
    max_pages: Optional[int] = None


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


def get_session():
    """Создает сессию со случайными настройками"""
    session = requests.Session()
    session.proxies = {
        "http": f"http://{random.choice(PROXIES)}",
        "https": f"http://{random.choice(PROXIES)}"
    }
    print(session)
    session.headers = {
        "User-Agent": UserAgent().random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "DNT": "1"
    }
    return session


def parse_job_card(card):
    """Парсит карточку вакансии и возвращает словарь с данными"""
    title_elem = card.find("h3", class_="base-search-card__title")
    company_elem = card.find("h4", class_="base-search-card__subtitle")
    location_elem = card.find("span", class_="job-search-card__location")
    link_elem = card.find("a", class_="base-card__full-link")
    posted=card.find("time")

    return {
        "title": title_elem.text.strip() if title_elem else "",
        "company": company_elem.text.strip() if company_elem else "",
        "location": location_elem.text.strip() if location_elem else "",
        "job_link": link_elem.get("href").split("?")[0] if link_elem else "",
        "posted_date": posted.get("datetime")if link_elem else "",
    }


def save_partial_results(task_id: str, jobs: list):
    try:
        result_json = json.dumps(jobs)
        finished_at = datetime.now().isoformat()

        conn = sqlite3.connect('scraper.db')
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tasks SET 
            status = ?, finished_at = ?, result_count = ?, result_json = ? 
            WHERE id = ?""",
            ("partial", finished_at, len(jobs), result_json, task_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"Saved partial results for task {task_id} ({len(jobs)} jobs)")
    except Exception as e:
        logger.error(f"Failed to save partial results: {str(e)}")


def init_db():
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()

    # Сначала проверяем существование таблицы
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
    table_exists = cursor.fetchone()

    if table_exists:
        # Проверяем наличие колонки period
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [column[1] for column in cursor.fetchall()]

        if 'period' not in columns:
            # Добавляем новую колонку
            cursor.execute("ALTER TABLE tasks ADD COLUMN period TEXT")
            logger.info("Added 'period' column to tasks table")

        if 'max_pages' not in columns:
            # Добавляем новую колонку (заменили max_jobs на max_pages)
            cursor.execute("ALTER TABLE tasks ADD COLUMN max_pages INTEGER")
            logger.info("Added 'max_pages' column to tasks table")
    else:
        # Создаем таблицу с новыми колонками
        cursor.execute('''
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT,
                created_at TEXT,
                finished_at TEXT,
                result_count INTEGER,
                result_json TEXT,
                keywords TEXT,
                location TEXT,
                period TEXT,
                max_pages INTEGER
            )
        ''')
        logger.info("Created new tasks table with all columns")

    conn.commit()
    conn.close()


init_db()


# Главная страница
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    if not os.path.exists("templates/index.html"):
        raise HTTPException(status_code=500, detail="index.html not found")

    # Получаем все результаты для таблицы
    all_jobs = await get_all_job_results()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "all_jobs": all_jobs
    })


async def get_all_job_results() -> List[Dict[str, Any]]:
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, created_at, result_json, keywords, location 
        FROM tasks WHERE (status = 'completed' OR status = 'partial') 
        AND result_json IS NOT NULL"""
    )
    tasks = cursor.fetchall()
    conn.close()

    all_jobs = []
    seen_links = set()

    for task in tasks:
        task_id, created_at, result_json, search_keywords, search_location = task
        try:
            jobs = json.loads(result_json)
            for job in jobs:
                # Извлекаем полную ссылку
                full_link = job.get('job_link') or job.get('link') or job.get('url')
                if not full_link:
                    continue

                # Проверяем уникальность ссылки
                if full_link in seen_links:
                    continue
                seen_links.add(full_link)

                job['task_id'] = task_id
                job['created_at'] = created_at
                job['search_keywords'] = search_keywords
                job['search_location'] = search_location
                job['job_link'] = full_link
                all_jobs.append(job)
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in task {task_id}")

    return all_jobs


@app.post("/api/scrape", response_model=ScrapeTask)
async def create_scrape_task(request: ScrapeRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat()

    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO tasks 
        (id, status, created_at, keywords, location, period, max_pages) 
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, "pending", created_at, request.keywords, request.location, request.period, request.max_pages)
    )
    conn.commit()
    conn.close()

    background_tasks.add_task(run_scraper_task, task_id, request)

    return {
        "id": task_id,
        "status": "pending",
        "created_at": created_at,
        "keywords": request.keywords,
        "location": request.location,
        "period": request.period,
        "max_pages": request.max_pages
    }


async def run_scraper_task(task_id: str, request: ScrapeRequest):
    try:
        logger.info(f"Starting scraping task {task_id} for period: {request.period}")
        update_task_status(task_id, "in_progress")

        jobs = []
        print(request.period)
        # Проверяем и устанавливаем период по умолчанию, если не указан
        if not request.period:
            request.period = "24h"  # Значение по умолчанию



        for start in range(0, 1000, 10):  # Note the added step=10
            try:
                url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{period_filter}keywords={quote(request.keywords)}&location={quote(request.location)}&start={start}"
                print(url)
                logger.debug(f"Fetching URL: {url}")

                session = get_session()
                response = session.get(url)
                response.raise_for_status()

                soup = BeautifulSoup(response.text, "lxml")
                cards = soup.find_all("div", class_="base-card")

                if not cards:
                    logger.info(f"No more jobs found at start={start}, stopping")
                    break

                page_added = 0
                for card in cards:
                    job_data = parse_job_card(card)
                    jobs.append(job_data)
                    page_added += 1

                logger.info(f"Page {start // 10 + 1}: Found {page_added} jobs (Total: {len(jobs)})")

                # Сохраняем промежуточные результаты каждые 2 страницы
                if start > 0 and start % 20 == 0:
                    save_partial_results(task_id, jobs)

                time.sleep(random.uniform(1, 3))

            except Exception as e:
                logger.error(f"Error at start={start}: {str(e)}")
                time.sleep(5)
                continue

        # Удаляем дубликаты по ссылке
        unique_jobs = []
        seen_links = set()
        for job in jobs:
            link = job.get('job_link')
            if link and link not in seen_links:
                seen_links.add(link)
                unique_jobs.append(job)

        result_json = json.dumps(unique_jobs)
        finished_at = datetime.now().isoformat()

        conn = sqlite3.connect('scraper.db')
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tasks SET 
            status = ?, finished_at = ?, result_count = ?, result_json = ? 
            WHERE id = ?""",
            ("completed", finished_at, len(unique_jobs), result_json, task_id)
        )
        conn.commit()
        conn.close()

        logger.info(f"Finished scraping task {task_id}, found {len(unique_jobs)} unique jobs")

    except Exception as e:
        logger.error(f"Error in task {task_id}: {str(e)}")
        update_task_status(task_id, "failed")
        if 'jobs' in locals() and jobs:
            save_partial_results(task_id, jobs)
        raise


def update_task_status(task_id: str, status: str):
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (status, task_id)
    )
    conn.commit()
    conn.close()
    logger.info(f"Updated task {task_id} status to {status}")


# Остальные endpoint'ы остаются без изменений
@app.get("/api/tasks", response_model=List[ScrapeTask])
async def get_all_tasks():
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, status, created_at, finished_at, 
           result_count, keywords, location, period, max_pages 
           FROM tasks ORDER BY created_at DESC"""
    )
    tasks = cursor.fetchall()
    conn.close()

    return [{
        "id": task[0],
        "status": task[1],
        "created_at": task[2],
        "finished_at": task[3],
        "result_count": task[4],
        "keywords": task[5],
        "location": task[6],
        "period": task[7],
        "max_pages": task[8]
    } for task in tasks]


@app.get("/api/tasks/{task_id}", response_model=ScrapeTask)
async def get_task_status(task_id: str):
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, status, created_at, finished_at, 
           result_count, keywords, location, period, max_pages 
           FROM tasks WHERE id = ?""",
        (task_id,)
    )
    task = cursor.fetchone()
    conn.close()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "id": task[0],
        "status": task[1],
        "created_at": task[2],
        "finished_at": task[3],
        "result_count": task[4],
        "keywords": task[5],
        "location": task[6],
        "period": task[7],
        "max_pages": task[8]
    }


@app.get("/api/tasks/{task_id}/results")
async def get_task_results(task_id: str):
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT result_json, keywords, location FROM tasks WHERE id = ? AND (status = 'completed' OR status = 'partial')",
        (task_id,)
    )
    result = cursor.fetchone()
    conn.close()

    if not result:
        raise HTTPException(status_code=404, detail="Results not available")

    result_json, search_keywords, search_location = result
    jobs = json.loads(result_json)

    # Добавляем информацию о задаче к каждой вакансии
    for job in jobs:
        job['task_id'] = task_id
        job['search_keywords'] = search_keywords
        job['search_location'] = search_location

    return JSONResponse(content=jobs)


@app.get("/api/all-results")
async def get_all_results():
    jobs = await get_all_job_results()
    return JSONResponse(content=jobs)


@app.delete("/api/jobs/delete")
async def delete_jobs(job_links: List[str]):
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()

    try:
        # Get all tasks with their results
        cursor.execute(
            "SELECT id, result_json FROM tasks WHERE (status = 'completed' OR status = 'partial') AND result_json IS NOT NULL")
        tasks = cursor.fetchall()

        deleted_count = 0

        for task_id, result_json in tasks:
            try:
                jobs = json.loads(result_json)
                # Filter out jobs to be deleted
                updated_jobs = [job for job in jobs if job.get('job_link') not in job_links and
                                job.get('link') not in job_links and
                                job.get('url') not in job_links]

                # If jobs were removed, update the task
                if len(updated_jobs) < len(jobs):
                    deleted_count += (len(jobs) - len(updated_jobs))
                    cursor.execute(
                        "UPDATE tasks SET result_json = ?, result_count = ? WHERE id = ?",
                        (json.dumps(updated_jobs), len(updated_jobs), task_id))

            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in task {task_id}")
                continue

        conn.commit()
        return {"message": f"Deleted {deleted_count} jobs", "deleted_count": deleted_count}

    finally:
        conn.close()


@app.get("/api/jobs/check-unique")
async def check_job_unique(job_link: str):
    conn = sqlite3.connect('scraper.db')
    cursor = conn.cursor()

    try:
        cursor.execute(
            "SELECT result_json FROM tasks WHERE (status = 'completed' OR status = 'partial') AND result_json IS NOT NULL")
        tasks = cursor.fetchall()

        for _, result_json in tasks:
            try:
                jobs = json.loads(result_json)
                for job in jobs:
                    if (job.get('job_link') == job_link or
                            job.get('link') == job_link or
                            job.get('url') == job_link):
                        return {"is_unique": False}
            except json.JSONDecodeError:
                continue

        return {"is_unique": True}
    finally:
        conn.close()


@app.get("/api/export-excel")
async def export_to_excel():
    jobs = await get_all_job_results()

    if not jobs:
        raise HTTPException(status_code=404, detail="No jobs found to export")

    # Создаем DataFrame
    df = pd.DataFrame(jobs)

    # Выбираем нужные колонки
    df = df[[
        'task_id',
        'created_at',
        'search_keywords',
        'search_location',
        'title',
        'company',
        'location',
        'job_link',
        'posted_date'
    ]]

    # Переименовываем колонки
    df = df.rename(columns={
        'task_id': 'Task ID',
        'created_at': 'Task Created',
        'search_keywords': 'Search Keywords',
        'search_location': 'Search Location',
        'title': 'Job Title',
        'company': 'Company',
        'location': 'Job Location',
        'job_link': 'Job Link',
        'posted_date': 'Posted Date'
    })

    # Создаем Excel файл в памяти
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        # Записываем данные
        df.to_excel(writer, sheet_name='LinkedIn Jobs', index=False)

        workbook = writer.book
        worksheet = writer.sheets['LinkedIn Jobs']

        # Формат для текста (без гиперссылки)
        text_format = workbook.add_format({'num_format': '@'})

        # Применяем текстовый формат ко всей колонке с ссылками
        worksheet.set_column('H:H', 100, text_format)

        # Вручную перезаписываем каждую ссылку как текст
        for row_num, url in enumerate(df['Job Link'], start=1):
            if pd.notna(url):
                worksheet.write_string(row_num, 7, str(url))  # Колонка H (индекс 7)

        # Устанавливаем ширину колонок
        worksheet.set_column('A:A', 36)  # Task ID
        worksheet.set_column('B:B', 20)  # Task Created
        worksheet.set_column('C:C', 30)  # Search Keywords
        worksheet.set_column('D:D', 25)  # Search Location
        worksheet.set_column('E:E', 30)  # Job Title
        worksheet.set_column('F:F', 25)  # Company
        worksheet.set_column('G:G', 25)  # Job Location
        worksheet.set_column('I:I', 15)  # Posted Date

    output.seek(0)

    # Возвращаем файл как поток
    headers = {
        'Content-Disposition': 'attachment; filename="linkedin_jobs_export.xlsx"',
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    }
    return StreamingResponse(output, headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
