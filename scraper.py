#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from playwright.async_api import BrowserContext, Page, async_playwright

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
PRIORITY_CONFIG_PATH = BASE_DIR / "lead_priority.json"
DEFAULT_SPREADSHEET_ID = "1QRnMXp8lTmIefhHnP5LdSoin1QMj_imewcf3RAh_KzI"
DEFAULT_CREDENTIALS_PATH = BASE_DIR / "credentials.json"
DEFAULT_OUTPUT_SHEET = "Leads_OAB_Scraper"
DEFAULT_NAME_COLUMN_INDEX = 1
DEFAULT_SECCIONAL = "Santa Catarina"
OAB_URL = "https://cna.oab.org.br/"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
PHONE_REGEX = re.compile(r"(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?(?:9\d{4}|\d{4})[-\s]?\d{4}")
EMAIL_REGEX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CACHE_MAX_AGE_HOURS = 6


def cleanup_cache(max_age_hours: float = CACHE_MAX_AGE_HOURS) -> int:
    CACHE_DIR.mkdir(exist_ok=True)
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in CACHE_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def cache_path(filename: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / filename


@dataclass
class ScraperConfig:
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH
    spreadsheet_id: str = DEFAULT_SPREADSHEET_ID
    output_sheet_name: str = DEFAULT_OUTPUT_SHEET
    name_column_index: int = DEFAULT_NAME_COLUMN_INDEX
    seccional: str = DEFAULT_SECCIONAL
    limit: int | None = None
    headless: bool = True
    min_delay: float = 1.2
    max_delay: float = 2.8


def default_logger(message: str) -> None:
    print(message, flush=True)


def log(log_callback: Callable[[str], None] | None, message: str) -> None:
    (log_callback or default_logger)(message)


def allowed_credential_file(filename: str) -> bool:
    return bool(filename) and Path(filename).suffix.lower() == ".json"


def load_service_account_info(credentials_path: Path) -> dict[str, Any]:
    if not credentials_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {credentials_path.name}")

    with credentials_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    required_keys = {"type", "client_email", "private_key", "project_id"}
    missing = sorted(required_keys - data.keys())
    if missing:
        raise ValueError(f"JSON inválido. Campos ausentes: {', '.join(missing)}")
    if data.get("type") != "service_account":
        raise ValueError("O arquivo enviado não é uma Service Account válida.")
    return data


def build_sheets_service(credentials_path: Path):
    credentials = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def normalize_spreadsheet_id(raw_value: str | None) -> str:
    spreadsheet_id = (raw_value or "").strip()
    return spreadsheet_id or DEFAULT_SPREADSHEET_ID


def inspect_spreadsheet(credentials_path: Path, spreadsheet_id: str) -> dict[str, Any]:
    info = load_service_account_info(credentials_path)
    service = build_sheets_service(credentials_path)

    spreadsheet = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="properties.title,sheets.properties.title,sheets.properties.gridProperties",
    ).execute()

    sheets = []
    for sheet in spreadsheet.get("sheets", []):
        properties = sheet.get("properties", {})
        grid = properties.get("gridProperties", {})
        sheets.append(
            {
                "title": properties.get("title", "Sem nome"),
                "rows": grid.get("rowCount", 0),
                "columns": grid.get("columnCount", 0),
            }
        )

    return {
        "connected": True,
        "client_email": info.get("client_email", ""),
        "project_id": info.get("project_id", ""),
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_title": spreadsheet.get("properties", {}).get("title", ""),
        "sheets": sheets,
    }


def is_white_or_default(bg_color: dict[str, float] | None) -> bool:
    if not bg_color:
        return True
    red = bg_color.get("red", 1.0)
    green = bg_color.get("green", 1.0)
    blue = bg_color.get("blue", 1.0)
    return red >= 0.92 and green >= 0.92 and blue >= 0.92


def find_first_matching_value(row_values: list[str], pattern: re.Pattern[str]) -> str:
    for value in row_values:
        match = pattern.search(value or "")
        if match:
            return match.group(0).strip()
    return ""


def extract_row_values(cells: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for cell in cells:
        values.append((cell or {}).get("formattedValue", "").strip())
    return values


def load_priority_config() -> dict[str, Any]:
    if not PRIORITY_CONFIG_PATH.exists():
        return {"order": [], "enabled": {}}
    try:
        data = json.loads(PRIORITY_CONFIG_PATH.read_text(encoding="utf-8"))
        return {
            "order": list(data.get("order", [])),
            "enabled": dict(data.get("enabled", {})),
        }
    except Exception:
        return {"order": [], "enabled": {}}


def save_priority_config(order: list[str], enabled: dict[str, bool]) -> dict[str, Any]:
    config = {"order": order, "enabled": enabled}
    PRIORITY_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def apply_priority(sheet_titles: list[str], config: dict[str, Any] | None = None) -> list[str]:
    cfg = config or load_priority_config()
    order = cfg.get("order", [])
    enabled = cfg.get("enabled", {})

    available = set(sheet_titles)
    ordered = [s for s in order if s in available]
    extras = [s for s in sheet_titles if s not in ordered]
    full = ordered + extras
    return [s for s in full if enabled.get(s, True)]


def get_white_leads(
    service,
    spreadsheet_id: str,
    *,
    output_sheet_name: str = DEFAULT_OUTPUT_SHEET,
    name_column_index: int = DEFAULT_NAME_COLUMN_INDEX,
    priority_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields=(
            "properties.title,"
            "sheets.properties.title,"
            "sheets.data.rowData.values.formattedValue,"
            "sheets.data.rowData.values.userEnteredFormat.backgroundColor"
        ),
    ).execute()

    sheets_by_title: dict[str, dict[str, Any]] = {}
    for sheet in spreadsheet.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        if title and title != output_sheet_name:
            sheets_by_title[title] = sheet

    cfg = priority_config if priority_config is not None else load_priority_config()
    ordered_titles = apply_priority(list(sheets_by_title.keys()), cfg)

    white_rows: list[dict[str, Any]] = []
    header_keywords = {"nome", "name", "advogado"}

    for sheet_title in ordered_titles:
        sheet = sheets_by_title.get(sheet_title)
        if not sheet:
            continue
        rows = sheet.get("data", [{}])[0].get("rowData", [])

        for row_index, row in enumerate(rows, start=1):
            cells = row.get("values", [])
            if len(cells) <= name_column_index:
                continue

            row_values = extract_row_values(cells)
            name_cell = cells[name_column_index] or {}
            raw_name = row_values[name_column_index].strip()
            normalized_name = raw_name.lower()

            if not raw_name or normalized_name in header_keywords:
                continue

            if not is_white_or_default(name_cell.get("userEnteredFormat", {}).get("backgroundColor")):
                continue

            white_rows.append(
                {
                    "source_sheet": sheet_title,
                    "source_row": row_index,
                    "source_range": f"{sheet_title}!A{row_index}",
                    "nome": raw_name,
                    "status_planilha": row_values[0] if row_values else "",
                    "telefone_planilha": find_first_matching_value(row_values, PHONE_REGEX),
                    "email_planilha": find_first_matching_value(row_values, EMAIL_REGEX),
                    "linha_original": row_values,
                }
            )

    return white_rows


def count_leads_per_sheet(service, spreadsheet_id: str, output_sheet_name: str = DEFAULT_OUTPUT_SHEET, name_column_index: int = DEFAULT_NAME_COLUMN_INDEX) -> dict[str, int]:
    spreadsheet = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields=(
            "sheets.properties.title,"
            "sheets.data.rowData.values.formattedValue,"
            "sheets.data.rowData.values.userEnteredFormat.backgroundColor"
        ),
    ).execute()

    header_keywords = {"nome", "name", "advogado"}
    counts: dict[str, int] = {}
    for sheet in spreadsheet.get("sheets", []):
        title = sheet.get("properties", {}).get("title", "")
        if not title or title == output_sheet_name:
            continue
        rows = sheet.get("data", [{}])[0].get("rowData", [])
        count = 0
        for row in rows:
            cells = row.get("values", [])
            if len(cells) <= name_column_index:
                continue
            row_values = extract_row_values(cells)
            name_cell = cells[name_column_index] or {}
            raw_name = row_values[name_column_index].strip()
            if not raw_name or raw_name.lower() in header_keywords:
                continue
            if not is_white_or_default(name_cell.get("userEnteredFormat", {}).get("backgroundColor")):
                continue
            count += 1
        counts[title] = count
    return counts


def preview_white_leads(config: ScraperConfig) -> dict[str, Any]:
    service = build_sheets_service(config.credentials_path)
    leads = get_white_leads(
        service,
        config.spreadsheet_id,
        output_sheet_name=config.output_sheet_name,
        name_column_index=config.name_column_index,
    )
    return {
        "total_white_rows": len(leads),
        "sample": leads[:10],
    }


async def setup_browser(
    playwright,
    headless: bool,
    log_callback: Callable[[str], None] | None = None,
) -> tuple[Any, BrowserContext]:
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    launch_profiles = [
        {
            "label": f"Chromium {'headless' if headless else 'headed'}",
            "kwargs": {"headless": headless, "args": launch_args},
        },
    ]

    if headless:
        launch_profiles.append(
            {
                "label": "Chromium headed fallback",
                "kwargs": {"headless": False, "args": launch_args},
            }
        )

    launch_profiles.append(
        {
            "label": f"Google Chrome channel {'headless' if headless else 'headed'}",
            "kwargs": {"headless": headless, "channel": "chrome", "args": launch_args},
        }
    )

    if headless:
        launch_profiles.append(
            {
                "label": "Google Chrome channel headed fallback",
                "kwargs": {"headless": False, "channel": "chrome", "args": launch_args},
            }
        )

    browser = None
    launch_errors: list[str] = []
    for profile in launch_profiles:
        try:
            log(log_callback, f"Tentando iniciar navegador: {profile['label']}")
            browser = await playwright.chromium.launch(**profile["kwargs"])
            log(log_callback, f"Navegador iniciado com sucesso: {profile['label']}")
            break
        except Exception as exc:
            message = str(exc).splitlines()[0][:220]
            launch_errors.append(f"{profile['label']}: {message}")
            log(log_callback, f"Falha ao iniciar {profile['label']}: {message}")

    if browser is None:
        joined = " | ".join(launch_errors) or "sem detalhes adicionais"
        raise RuntimeError(
            "Não foi possível iniciar um navegador Playwright compatível. "
            "Se estiver usando Python 3.14 no macOS, prefira Python 3.12/3.13 no venv. "
            f"Tentativas: {joined}"
        )

    context = await browser.new_context(
        viewport={"width": 1440, "height": 940},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


async def random_pause(min_delay: float, max_delay: float) -> None:
    await asyncio.sleep(random.uniform(min_delay, max_delay))


SECCIONAL_UF_MAP: dict[str, str] = {
    "acre": "AC", "alagoas": "AL", "amapá": "AP", "amazonas": "AM",
    "bahia": "BA", "ceará": "CE", "distrito federal": "DF",
    "espírito santo": "ES", "goiás": "GO", "maranhão": "MA",
    "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG",
    "pará": "PA", "paraíba": "PB", "paraná": "PR", "pernambuco": "PE",
    "piauí": "PI", "rio de janeiro": "RJ", "rio grande do norte": "RN",
    "rio grande do sul": "RS", "rondônia": "RO", "roraima": "RR",
    "santa catarina": "SC", "são paulo": "SP", "sergipe": "SE",
    "tocantins": "TO",
}


def seccional_to_uf(seccional: str) -> str:
    if len(seccional) == 2:
        return seccional.upper()
    return SECCIONAL_UF_MAP.get(seccional.strip().lower(), "")


async def wait_for_captcha(page: Page, log_callback: Callable[[str], None] | None = None) -> bool:
    modal = page.locator("text=Confirme que você não é um robô")
    try:
        if await modal.is_visible():
            log(log_callback, "⏳ CAPTCHA — resolva no navegador aberto")
            await modal.wait_for(state="hidden", timeout=300_000)
            log(log_callback, "CAPTCHA resolvido, continuando...")
            await asyncio.sleep(1)
            return True
    except Exception:
        pass
    return False


async def search_lawyer(
    page: Page,
    nome: str,
    *,
    seccional: str,
    min_delay: float,
    max_delay: float,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    seccional_label = f"Conselho Seccional - {seccional}" if len(seccional) > 2 else ""
    api_search_data: dict[str, Any] = {}
    api_detail_data: dict[str, Any] = {}

    async def on_response(response):
        nonlocal api_search_data, api_detail_data
        try:
            if "/api/advogado/search" in response.url and response.status == 200:
                api_search_data = await response.json()
            elif "/api/advogado/detail" in response.url and response.status == 200:
                api_detail_data = await response.json()
        except Exception:
            pass

    page.on("response", on_response)

    try:
        for attempt in range(3):
            api_search_data.clear()
            api_detail_data.clear()

            if "cna.oab.org.br" not in page.url:
                await page.goto(OAB_URL, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(1.0)

            nome_input = page.locator("input[name='name']")
            reg_input = page.locator("input[name='registration']")

            await nome_input.fill("")
            await reg_input.fill("")
            await nome_input.fill(nome)

            if seccional_label:
                try:
                    await page.locator("select[name='sectional']").select_option(label=seccional_label)
                except Exception:
                    pass

            pesquisar_btn = page.locator("button:has-text('Pesquisar')")
            await pesquisar_btn.wait_for(state="visible", timeout=5000)

            for _ in range(10):
                if not await pesquisar_btn.is_disabled():
                    break
                resolved = await wait_for_captcha(page, log_callback)
                if resolved:
                    break
                await asyncio.sleep(1)

            await pesquisar_btn.click(timeout=10000, force=True)
            await asyncio.sleep(2)

            await wait_for_captcha(page, log_callback)

            for _ in range(8):
                if api_search_data:
                    break
                await asyncio.sleep(1)

            if api_search_data:
                break

            if attempt < 2:
                log(log_callback, "    Sem resposta da API, tentando novamente...")
                await page.goto(OAB_URL, wait_until="networkidle", timeout=30000)
                await asyncio.sleep(1)

        if not api_search_data:
            return {"nome": nome, "encontrado": False, "erro": "sem_resposta_api"}

        items = api_search_data.get("items", [])
        if not items:
            return {"nome": nome, "encontrado": False, "erro": "nenhum_resultado"}

        best = items[0]
        parametro = best.get("parametro", "")

        if parametro:
            clickable = page.locator("li.cursor-pointer, li[class*='hover']").first
            try:
                await clickable.wait_for(state="visible", timeout=5000)
                await clickable.click(timeout=5000)
                for _ in range(8):
                    if api_detail_data:
                        break
                    await asyncio.sleep(1)
                if not api_detail_data:
                    await wait_for_captcha(page, log_callback)
                    for _ in range(5):
                        if api_detail_data:
                            break
                        await asyncio.sleep(1)
            except Exception:
                pass

        detail = api_detail_data.get("data", {}) if api_detail_data.get("success") else {}

        result = {
            "nome": nome,
            "encontrado": True,
            "oab_num": detail.get("inscricao") or best.get("inscricao", ""),
            "telefone_oab": detail.get("telefone", ""),
            "erro": "" if detail else "sem_detalhe",
        }

        await page.goto(OAB_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(0.5)
        await random_pause(min_delay, max_delay)

        return result
    except Exception as exc:
        return {"nome": nome, "encontrado": False, "erro": str(exc)[:200]}
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def warmup_captcha(page: Page, seccional: str, log_callback: Callable[[str], None] | None = None) -> None:
    seccional_label = f"Conselho Seccional - {seccional}" if len(seccional) > 2 else ""
    log(log_callback, "Aquecendo sessão...")

    await page.locator("input[name='name']").fill("TESTE")
    if seccional_label:
        try:
            await page.locator("select[name='sectional']").select_option(label=seccional_label)
        except Exception:
            pass
    await page.locator("button:has-text('Pesquisar')").click(timeout=5000)
    await asyncio.sleep(2)
    await wait_for_captcha(page, log_callback)
    await page.goto(OAB_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(1)
    log(log_callback, "Sessão pronta — scraping iniciando")


async def scrape_all_lawyers(
    leads: list[dict[str, Any]],
    *,
    seccional: str,
    headless: bool,
    min_delay: float,
    max_delay: float,
    log_callback: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        browser, context = await setup_browser(playwright, headless=False, log_callback=log_callback)
        page = await context.new_page()
        await page.goto(OAB_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(1.0)

        await warmup_captcha(page, seccional, log_callback)

        for index, lead in enumerate(leads, start=1):
            if stop_check and stop_check():
                log(log_callback, "Processamento interrompido pelo usuário")
                break

            nome = lead["nome"]
            log(log_callback, f"[{index}/{len(leads)}] Consultando {nome}")
            result = await search_lawyer(
                page,
                nome,
                seccional=seccional,
                min_delay=min_delay,
                max_delay=max_delay,
                log_callback=log_callback,
            )
            merged = {**lead, **result}
            results.append(merged)

            tel = result.get("telefone_oab", "") or "—"
            oab = result.get("oab_num", "") or "—"
            erro = result.get("erro", "")
            if result.get("encontrado"):
                log(log_callback, f"    ✓ OAB: {oab} | Tel: {tel}")
            else:
                log(log_callback, f"    ✗ {erro}")

        await browser.close()

    return results


def ensure_output_sheet(service, spreadsheet_id: str, output_sheet_name: str) -> None:
    metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = [sheet["properties"]["title"] for sheet in metadata.get("sheets", [])]
    if output_sheet_name in existing:
        return

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": output_sheet_name}}}]},
    ).execute()


def write_results(
    service,
    spreadsheet_id: str,
    output_sheet_name: str,
    results: list[dict[str, Any]],
) -> None:
    ensure_output_sheet(service, spreadsheet_id, output_sheet_name)

    headers = [
        "Nome",
        "Nº OAB",
        "Telefone OAB",
        "Telefone Planilha",
        "Encontrado",
        "Erro",
    ]

    rows = [headers]
    for result in results:
        rows.append(
            [
                result.get("nome", ""),
                result.get("oab_num", ""),
                result.get("telefone_oab", ""),
                result.get("telefone_planilha", ""),
                "SIM" if result.get("encontrado") else "NÃO",
                result.get("erro", ""),
            ]
        )

    clear_range = f"{output_sheet_name}!A:Z"
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=clear_range,
        body={},
    ).execute()

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{output_sheet_name}!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def save_json_backup(results: list[dict[str, Any]], output_path: Path | None = None) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    path = output_path or (CACHE_DIR / "resultados_oab.json")
    slim = [
        {
            "nome": r.get("nome", ""),
            "oab_num": r.get("oab_num", ""),
            "telefone_oab": r.get("telefone_oab", ""),
            "encontrado": r.get("encontrado", False),
            "erro": r.get("erro", ""),
        }
        for r in results
    ]
    with path.open("w", encoding="utf-8") as file:
        json.dump(slim, file, ensure_ascii=False, indent=2)
    return path


def run_scrape(
    config: ScraperConfig,
    *,
    log_callback: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    removed = cleanup_cache()
    if removed:
        log(log_callback, f"Cache limpo: {removed} arquivo(s) antigo(s) removido(s)")
    log(log_callback, "Autenticando no Google Sheets")
    service = build_sheets_service(config.credentials_path)
    spreadsheet_meta = inspect_spreadsheet(config.credentials_path, config.spreadsheet_id)

    log(log_callback, "Lendo planilha e filtrando linhas com fundo branco")
    leads = get_white_leads(
        service,
        config.spreadsheet_id,
        output_sheet_name=config.output_sheet_name,
        name_column_index=config.name_column_index,
    )
    total_white_rows = len(leads)
    log(log_callback, f"Linhas brancas encontradas: {total_white_rows}")

    if config.limit is not None:
        leads = leads[: config.limit]
        log(log_callback, f"Aplicando limite manual: {len(leads)} leads")

    if not leads:
        return {
            "ok": True,
            "spreadsheet": spreadsheet_meta,
            "processed": 0,
            "found": 0,
            "total_white_rows": total_white_rows,
            "output_sheet_name": config.output_sheet_name,
            "backup_path": None,
            "results": [],
            "message": "Nenhuma linha branca disponível para processamento.",
        }

    log(log_callback, f"Iniciando scraping de {len(leads)} advogado(s) no CNA OAB")
    results = asyncio.run(
        scrape_all_lawyers(
            leads,
            seccional=config.seccional,
            headless=config.headless,
            min_delay=config.min_delay,
            max_delay=config.max_delay,
            log_callback=log_callback,
            stop_check=stop_check,
        )
    )

    log(log_callback, f"Escrevendo {len(results)} resultado(s) na aba {config.output_sheet_name}")
    write_results(service, config.spreadsheet_id, config.output_sheet_name, results)

    backup_path = save_json_backup(results)
    found = sum(1 for result in results if result.get("encontrado"))
    log(log_callback, f"Backup salvo em {backup_path.name}")
    log(log_callback, f"Processamento concluído: {found}/{len(results)} encontrados")

    return {
        "ok": True,
        "spreadsheet": spreadsheet_meta,
        "processed": len(results),
        "found": found,
        "total_white_rows": total_white_rows,
        "output_sheet_name": config.output_sheet_name,
        "backup_path": str(backup_path),
        "results": results,
        "message": "Processamento finalizado com sucesso.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scraper OABPrev com Google Sheets + CNA OAB")
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS_PATH), help="Caminho do credentials.json")
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID, help="ID da planilha Google Sheets")
    parser.add_argument("--output-sheet", default=DEFAULT_OUTPUT_SHEET, help="Nome da aba de saída")
    parser.add_argument("--name-column-index", type=int, default=DEFAULT_NAME_COLUMN_INDEX, help="Índice da coluna com o nome")
    parser.add_argument("--seccional", default=DEFAULT_SECCIONAL, help="Seccional usada na consulta do CNA")
    parser.add_argument("--limit", type=int, default=None, help="Limite de leads processados")
    parser.add_argument("--show-browser", action="store_true", help="Executa o Playwright com navegador visível")
    args = parser.parse_args()

    config = ScraperConfig(
        credentials_path=Path(args.credentials).expanduser().resolve(),
        spreadsheet_id=normalize_spreadsheet_id(args.spreadsheet_id),
        output_sheet_name=args.output_sheet,
        name_column_index=args.name_column_index,
        seccional=args.seccional,
        limit=args.limit,
        headless=not args.show_browser,
    )

    try:
        summary = run_scrape(config)
        print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))
    except HttpError as exc:
        status = getattr(exc.resp, "status", 500)
        if status == 403:
            raise SystemExit("A Service Account não tem acesso à planilha. Compartilhe a planilha com o client_email do JSON.") from exc
        if status == 404:
            raise SystemExit("Planilha não encontrada. Verifique o Spreadsheet ID informado.") from exc
        raise SystemExit(f"Erro da API Google: {exc}") from exc
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
