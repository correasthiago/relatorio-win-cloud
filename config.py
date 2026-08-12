# -*- coding: utf-8 -*-
"""
Configurações do Relatório Diário de Trading — versão NUVEM (GitHub Actions).

IMPORTANTE: este arquivo é público (vai pro GitHub). Por isso, NENHUM segredo
(chave de API, token, etc.) fica escrito aqui — tudo vem de variáveis de
ambiente, que no GitHub Actions são preenchidas a partir dos "Secrets" do
repositório (Settings > Secrets and variables > Actions). Veja o
SETUP_NUVEM.md para o passo a passo de como cadastrar os 3 segredos.

Motor principal: Google Gemini (API gratuita).
Motor de reserva (fallback): Ollama local — não existe na nuvem, então esse
fallback só entra em ação se você também rodar uma cópia deste projeto na
sua própria máquina com Ollama instalado; na nuvem, se o Gemini falhar, o
script simplesmente registra o erro no log e não envia nada nesse ciclo.
"""

import os

# --- IA principal: Google Gemini (gratuito) ---
# Vem do Secret "GEMINI_API_KEY" cadastrado no GitHub.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

# --- IA de reserva (fallback) — normalmente inativo na nuvem, ver nota acima ---
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

# --- Breaking News (alertas em tempo real, fora da grade normal de horários) ---
# Palavras-chave de alto impacto pra WIN/WDO — pode editar essa lista à vontade.
PALAVRAS_CHAVE_BREAKING = [
    "copom", "fed", "payroll", "ipca", "cpi", "governo", "taxação", "taxacao",
    "guerra", "petrobras", "vale", "presidente", "lula", "powell", "decisão",
    "decisao", "urgente", "declaração", "declaracao",
]

BREAKING_JANELA_MINUTOS = 15  # olha notícias publicadas nos últimos X minutos
BREAKING_HORA_INICIO = "06:00"  # só verifica breaking news dentro desse horário (fuso São Paulo)
BREAKING_HORA_FIM = "20:30"

# --- Envio via Telegram ---
ENVIAR_TELEGRAM = True

# Vêm dos Secrets "TELEGRAM_BOT_TOKEN" e "TELEGRAM_CHAT_ID" cadastrados no GitHub.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Envio de e-mail (opcional, deixe False se for usar só Telegram) ---
ENVIAR_EMAIL = False

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

EMAIL_REMETENTE = os.environ.get("EMAIL_REMETENTE", "")
EMAIL_SENHA = os.environ.get("EMAIL_SENHA", "")
EMAIL_DESTINATARIO = os.environ.get("EMAIL_DESTINATARIO", "")
