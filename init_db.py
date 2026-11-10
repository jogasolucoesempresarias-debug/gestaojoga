"""Inicializa/atualiza o banco gestao_joga. Rode: python -X utf8 init_db.py

Idempotente. O servidor já roda a mesma migração sozinho ao subir (schema.migrar); este script
fica para preparar o banco à mão (ex.: ambiente novo, antes do 1º start).
"""
import os

import psycopg2
from dotenv import load_dotenv

import schema

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'),
    port=os.getenv('DB_PORT', '5432'),
    dbname=os.getenv('DB_NAME', 'gestao_joga'),
    user=os.getenv('DB_USER', 'postgres'),
    password=os.getenv('DB_PASSWORD', ''),
)
try:
    schema.migrar(conn)
    conn.commit()
finally:
    conn.close()
print("[OK] Banco pronto. Rode: python -X utf8 server.py")
