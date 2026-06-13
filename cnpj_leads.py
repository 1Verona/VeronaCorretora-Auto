#!/usr/bin/env python3
"""Gera leads de advogados/escritórios ativos a partir dos Dados Abertos de CNPJ
(Receita Federal), filtrando por UF e CNAE de advocacia.

Fonte dos dados: dataset público `basedosdados.br_me_cnpj` no BigQuery (Base dos
Dados), que republica os Dados Abertos do CNPJ da Receita Federal já em formato
tabular. Isso evita depender do antigo portal de download em zip da Receita (que
migrou para um Nextcloud sem listagem automatizável) e permite consultas
incrementais 100% automáticas.

Requer que a service account usada (credentials.json) tenha a permissão
`bigquery.jobs.create` no projeto de cobrança (roles/bigquery.user é suficiente).
A consulta em si lê dados públicos (sem custo, dentro do free tier de 1 TB/mês).
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

from google.cloud import bigquery
from google.oauth2 import service_account

from leads_sheet_writer import write_leads_sheet
from scraper import (
    DEFAULT_CREDENTIALS_PATH,
    DEFAULT_SPREADSHEET_ID,
    build_sheets_service,
    normalize_spreadsheet_id,
)

DEFAULT_OUTPUT_SHEET = "Leads_CNPJ_SC"
DEFAULT_UF = "SC"
DEFAULT_CNAE_TARGETS = {"6911701"}  # Serviços advocatícios
SITUACAO_ATIVA = ["2", "02"]

BD_PROJECT = "basedosdados"
EST_DATASET = "br_me_cnpj"
EST_TABLE = "estabelecimentos"
EMP_TABLE = "empresas"
MUNICIPIO_DATASET = "br_bd_diretorios_brasil"
MUNICIPIO_TABLE = "municipio"

HEADERS = [
    "Status",
    "Nome",
    "Telefone",
    "Email",
    "Endereço",
    "Município",
    "CNAE",
    "Data Início Atividade",
    "Anos Atuação",
    "Prioridade",
    "CNPJ",
]

MAIN_QUERY = f"""
WITH latest_empresas AS (
  SELECT cnpj_basico, razao_social
  FROM `{BD_PROJECT}.{EST_DATASET}.{EMP_TABLE}`
  WHERE data = @data_emp
)
SELECT
  est.cnpj_basico,
  est.cnpj_ordem,
  est.cnpj_dv,
  est.nome_fantasia,
  est.data_inicio_atividade,
  est.cnae_fiscal_principal,
  est.ddd_1,
  est.telefone_1,
  est.ddd_2,
  est.telefone_2,
  est.email,
  est.tipo_logradouro,
  est.logradouro,
  est.numero,
  est.bairro,
  est.cep,
  mun.nome AS municipio_nome,
  emp.razao_social
FROM `{BD_PROJECT}.{EST_DATASET}.{EST_TABLE}` AS est
LEFT JOIN latest_empresas AS emp ON emp.cnpj_basico = est.cnpj_basico
LEFT JOIN `{BD_PROJECT}.{MUNICIPIO_DATASET}.{MUNICIPIO_TABLE}` AS mun
  ON mun.id_municipio = est.id_municipio
WHERE est.data = @data_est
  AND est.sigla_uf = @uf
  AND est.situacao_cadastral IN UNNEST(@situacoes)
  AND (
    est.cnae_fiscal_principal IN UNNEST(@cnaes)
    OR EXISTS (
      SELECT 1 FROM UNNEST(SPLIT(IFNULL(est.cnae_fiscal_secundaria, ''), ',')) AS sec
      WHERE TRIM(sec) IN UNNEST(@cnaes)
    )
  )
"""

LATEST_DATA_QUERY = f"""
SELECT MAX(data) AS latest
FROM `{BD_PROJECT}.{EST_DATASET}.{{table}}`
WHERE data BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 24 MONTH) AND CURRENT_DATE()
"""


def build_bq_client(credentials_path: Path, bq_project: str | None) -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(str(credentials_path))
    return bigquery.Client(credentials=creds, project=bq_project or creds.project_id)


def latest_data_date(client: bigquery.Client, table_name: str) -> date | None:
    job = client.query(LATEST_DATA_QUERY.format(table=table_name))
    row = next(iter(job.result()), None)
    if row is None or not row.latest:
        return None
    return row.latest


def classify_priority(data_inicio: date | None, *, alta_max_anos: float, media_max_anos: float) -> tuple[str, float | None]:
    if data_inicio is None:
        return "MEDIA", None
    anos = (date.today() - data_inicio).days / 365.25
    if anos <= alta_max_anos:
        return "ALTA", round(anos, 1)
    if anos <= media_max_anos:
        return "MEDIA", round(anos, 1)
    return "BAIXA", round(anos, 1)


def format_cnpj(basico: str, ordem: str, dv: str) -> str:
    digits = f"{basico}{ordem}{dv}"
    if len(digits) != 14:
        return digits
    return f"{digits[0:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:14]}"


def format_phone(ddd: str | None, numero: str | None) -> str:
    ddd = (ddd or "").strip()
    numero = (numero or "").strip()
    if not numero:
        return ""
    return f"({ddd}) {numero}" if ddd else numero


def format_address(row: Any) -> str:
    parts: list[str] = []
    logradouro = " ".join(p for p in ((row.tipo_logradouro or "").strip(), (row.logradouro or "").strip()) if p)
    if logradouro:
        numero = (row.numero or "").strip()
        if numero and numero.upper() != "S/N":
            logradouro = f"{logradouro}, {numero}"
        parts.append(logradouro)
    if (row.bairro or "").strip():
        parts.append(row.bairro.strip())
    if (row.cep or "").strip():
        parts.append(row.cep.strip())
    return " - ".join(parts)


def build_lead_rows(rows: list[Any], *, alta_max_anos: float, media_max_anos: float) -> list[list[str]]:
    priority_order = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2}
    entries: list[dict[str, Any]] = []

    for row in rows:
        razao_social = (row.razao_social or "").strip()
        nome_fantasia = (row.nome_fantasia or "").strip()

        nome = razao_social or nome_fantasia
        if nome_fantasia and nome_fantasia != nome:
            nome = f"{nome} ({nome_fantasia})"
        if not nome:
            continue

        telefone = format_phone(row.ddd_1, row.telefone_1) or format_phone(row.ddd_2, row.telefone_2)
        email = (row.email or "").strip()
        endereco = format_address(row)
        municipio_nome = (row.municipio_nome or "").strip()
        cnae = (row.cnae_fiscal_principal or "").strip()
        data_inicio = row.data_inicio_atividade

        prioridade, anos = classify_priority(data_inicio, alta_max_anos=alta_max_anos, media_max_anos=media_max_anos)
        data_inicio_fmt = data_inicio.strftime("%d/%m/%Y") if data_inicio else ""

        cnpj = format_cnpj(row.cnpj_basico, row.cnpj_ordem, row.cnpj_dv)

        entries.append(
            {
                "row": [
                    "",
                    nome,
                    telefone,
                    email,
                    endereco,
                    municipio_nome,
                    cnae,
                    data_inicio_fmt,
                    f"{anos:.1f}" if anos is not None else "",
                    prioridade,
                    cnpj,
                ],
                "prioridade": prioridade,
                "nome": nome,
            }
        )

    entries.sort(key=lambda e: (priority_order.get(e["prioridade"], 1), e["nome"]))
    return [e["row"] for e in entries]


def run(args: argparse.Namespace) -> None:
    client = build_bq_client(args.credentials, args.bq_project)

    print("Identificando snapshot mais recente da base de CNPJ...")
    data_est = latest_data_date(client, EST_TABLE)
    data_emp = latest_data_date(client, EMP_TABLE)
    if data_est is None or data_emp is None:
        raise RuntimeError("Não foi possível determinar o snapshot mais recente da base de CNPJ.")
    print(f"  Estabelecimentos: {data_est.isoformat()} | Empresas: {data_emp.isoformat()}")

    if args.list_only:
        return

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("data_est", "DATE", data_est),
            bigquery.ScalarQueryParameter("data_emp", "DATE", data_emp),
            bigquery.ScalarQueryParameter("uf", "STRING", args.uf),
            bigquery.ArrayQueryParameter("situacoes", "STRING", SITUACAO_ATIVA),
            bigquery.ArrayQueryParameter("cnaes", "STRING", sorted(args.cnae)),
        ]
    )
    print(f"Consultando estabelecimentos ativos em {args.uf} com CNAE {sorted(args.cnae)}...")
    job = client.query(MAIN_QUERY, job_config=job_config)
    result_rows = list(job.result())
    print(f"  {len(result_rows)} estabelecimento(s) encontrados")
    print(f"  Bytes processados: {(job.total_bytes_processed or 0) / 1_048_576:.1f} MB")

    if args.limit:
        result_rows = result_rows[: args.limit]
        print(f"Aplicando limite manual: {len(result_rows)} registro(s)")

    rows = build_lead_rows(result_rows, alta_max_anos=args.alta_max_anos, media_max_anos=args.media_max_anos)
    print(f"Leads montados: {len(rows)}")

    service = build_sheets_service(args.credentials)
    result = write_leads_sheet(service, args.spreadsheet_id, args.output_sheet, HEADERS, rows, "CNPJ")
    print(f"Aba '{args.output_sheet}': {result['added']} novo(s), {result['skipped']} já existente(s) (total processado: {result['total_input']})")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera leads de advogados ativos a partir dos Dados Abertos de CNPJ (via BigQuery)")
    parser.add_argument("--uf", default=DEFAULT_UF, help="UF para filtrar (default: SC)")
    parser.add_argument(
        "--cnae",
        default=",".join(sorted(DEFAULT_CNAE_TARGETS)),
        help="Códigos CNAE (separados por vírgula) considerados advocacia (default: 6911701)",
    )
    parser.add_argument("--alta-max-anos", type=float, default=3.0, help="CNPJ com até X anos = prioridade ALTA (default: 3)")
    parser.add_argument("--media-max-anos", type=float, default=15.0, help="CNPJ com até X anos = prioridade MEDIA (default: 15)")
    parser.add_argument("--spreadsheet-id", default=DEFAULT_SPREADSHEET_ID)
    parser.add_argument("--output-sheet", default=DEFAULT_OUTPUT_SHEET)
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS_PATH))
    parser.add_argument("--bq-project", default=None, help="Projeto de cobrança do BigQuery (default: projeto da service account)")
    parser.add_argument("--list-only", action="store_true", help="Apenas identificar o snapshot mais recente, sem consultar/escrever leads")
    parser.add_argument("--limit", type=int, default=None, help="Limite de leads processados (para testes)")

    args = parser.parse_args(argv)
    args.uf = args.uf.strip().upper()
    args.cnae = {c.strip() for c in args.cnae.split(",") if c.strip()}
    args.credentials = Path(args.credentials).expanduser().resolve()
    args.spreadsheet_id = normalize_spreadsheet_id(args.spreadsheet_id)
    return args


def main() -> None:
    args = parse_args()
    try:
        run(args)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
