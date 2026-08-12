#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Relatório Diário de Notícias para Day Trade (foco em WIN - Mini Índice Bovespa)
=================================================================================

100% GRATUITO — usa a API gratuita do Google Gemini como motor principal.
Se o Gemini falhar por qualquer motivo (sem internet, chave inválida, etc.), o
script cai automaticamente para o Ollama (IA local, também gratuita) como reserva.

O que este script faz:
1. Coleta manchetes recentes de várias fontes de notícias (RSS) — mercado
   financeiro, política nacional, geopolítica internacional.
2. Envia essas manchetes para a IA (Gemini, ou Ollama se o Gemini falhar) com
   um prompt que pede para filtrar apenas o que é relevante para quem opera
   WIN (e WDO/DOL), e organizar tudo em um relatório estruturado e acionável.
3. Envia o relatório final para o Telegram (ou salva localmente, se preferir).

Existem 5 TIPOS de execução, escolhidos por parâmetro na linha de comando —
pensados pra rodar automaticamente em horários diferentes do dia (fuso
America/Sao_Paulo):

    python relatorio_diario.py abertura      # 07:00 — relatório completo (pré-mercado)
    python relatorio_diario.py atualizacao   # 08:30 e 09:45 — boletim curto, só o que mudou
    python relatorio_diario.py pos_ny        # 10:40 — pós-abertura de Nova York / tendência
    python relatorio_diario.py fechamento    # 20:00 — resumo de fechamento do pregão
    python relatorio_diario.py breaking      # a cada poucos minutos — alerta de notícia urgente

Se nenhum tipo for passado, o padrão é "abertura".

O modo "breaking" é diferente dos outros: em vez de montar um relatório
agregando várias notícias, ele verifica notícias muito recentes (últimos
minutos) uma a uma, filtra por palavras-chave de alto impacto (Copom, Fed,
Payroll, IPCA, Petrobras, Vale, etc.) e dispara um alerta imediato pra cada
notícia nova que bater no filtro — pensado pra ser chamado a cada poucos
minutos (ex: pelo Agendador de Tarefas do Windows), não uma vez por dia.

Como usar (veja o README.md para o passo a passo bem detalhado):
1. Pegue sua chave gratuita do Gemini em https://aistudio.google.com/apikey
2. Cole a chave em config.py, na linha GEMINI_API_KEY
3. pip install -r requirements.txt
4. Preencha config.py com o token do seu bot do Telegram
5. Rode: python relatorio_diario.py abertura

Este script NÃO executa ordens na bolsa nem dá recomendação de investimento.
Ele apenas organiza informação pública para apoiar sua própria análise.
"""

import os
import re
import sys
import time
import json
import difflib
import smtplib
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass

import feedparser
import requests

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("relatorio_diario")

FUSO_HORARIO = ZoneInfo("America/Sao_Paulo")


def _agora() -> datetime:
    """Hora atual sempre no fuso de São Paulo, independente do fuso do computador/servidor."""
    return datetime.now(FUSO_HORARIO)


# ---------------------------------------------------------------------------
# 1. FONTES DE NOTÍCIAS (RSS)
# ---------------------------------------------------------------------------
# Cada fonte é (nome, url_rss, categoria). Categoria só é usada para
# organizar o prompt enviado à IA, ajuda a dar contexto.

RSS_FEEDS = [
    # --- Mercado financeiro / Economia Brasil ---
    ("InfoMoney", "https://www.infomoney.com.br/feed/", "mercado_br"),
    ("Money Times", "https://www.moneytimes.com.br/feed/", "mercado_br"),
    ("Investing.com Brasil", "https://br.investing.com/rss/news.rss", "mercado_br"),
    ("Banco Central do Brasil - Notas à Imprensa", "https://www.bcb.gov.br/api/feed/sitebcb/notasimprensa", "mercado_br"),
    ("Agência Brasil - Economia", "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml", "mercado_br"),
    ("Valor Econômico", "https://valor.globo.com/rss/valor/", "mercado_br"),
    ("CNN Brasil", "https://www.cnnbrasil.com.br/feed/", "mercado_br"),
    ("G1 Economia", "https://g1.globo.com/rss/g1/economia/", "mercado_br"),

    # --- Política Brasil ---
    ("Agência Brasil - Geral", "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml", "politica_br"),
    ("G1 Política", "https://g1.globo.com/rss/g1/politica/", "politica_br"),
    ("Poder360", "https://www.poder360.com.br/feed/", "politica_br"),

    # --- Internacional / Geopolítica / Mercado global ---
    ("Investing.com Economy News", "https://www.investing.com/rss/news_14.rss", "internacional"),
    ("BBC Brasil", "https://feeds.bbci.co.uk/portuguese/rss.xml", "internacional"),
]

FEED_TIMEOUT_SEGUNDOS = 10
FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; RelatorioDiarioBot/1.0)"}
SIMILARIDADE_DUPLICATA = 0.85  # 0-1; quanto maior, mais "parecido" precisa ser pra contar como duplicata


@dataclass
class NewsItem:
    fonte: str
    categoria: str
    titulo: str
    resumo: str
    link: str
    publicado: datetime


def _parse_entry_date(entry) -> datetime:
    """Tenta extrair a data de publicação da entrada RSS; usa agora() se falhar."""
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime.fromtimestamp(time.mktime(val), tz=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _titulo_normalizado(titulo: str) -> str:
    """Normaliza um título pra comparação de duplicatas (minúsculo, sem pontuação/espaços extras)."""
    t = titulo.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _eh_duplicado(titulo_norm: str, titulos_vistos: list[str]) -> bool:
    """Compara por similaridade básica de texto contra os títulos já coletados."""
    for outro in titulos_vistos:
        if difflib.SequenceMatcher(None, titulo_norm, outro).ratio() >= SIMILARIDADE_DUPLICATA:
            return True
    return False


def _buscar_feed(url: str):
    """Baixa e faz o parse de um feed RSS, com timeout curto pra não travar o script."""
    resp = requests.get(url, timeout=FEED_TIMEOUT_SEGUNDOS, headers=FEED_HEADERS)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def coletar_noticias(janela_horas: float = 24) -> list[NewsItem]:
    """Percorre todos os feeds RSS e retorna itens publicados dentro da janela de horas,
    já sem duplicatas (por similaridade de título). Um feed fora do ar nunca trava o script."""
    corte = datetime.now(timezone.utc) - timedelta(hours=janela_horas)
    itens: list[NewsItem] = []
    titulos_vistos: list[str] = []

    for nome_fonte, url, categoria in RSS_FEEDS:
        try:
            log.info(f"Buscando feed: {nome_fonte}")
            feed = _buscar_feed(url)
        except Exception as e:
            log.warning(f"  -> Feed '{nome_fonte}' fora do ar ou lento demais, pulando ({e})")
            continue

        try:
            if feed.bozo and not feed.entries:
                log.warning(f"  -> Falha ao ler feed de {nome_fonte}: {getattr(feed, 'bozo_exception', 'formato inválido')}")
                continue

            for entry in feed.entries:
                pub = _parse_entry_date(entry)
                if pub < corte:
                    continue
                titulo = getattr(entry, "title", "").strip()
                if not titulo:
                    continue

                titulo_norm = _titulo_normalizado(titulo)
                if _eh_duplicado(titulo_norm, titulos_vistos):
                    continue
                titulos_vistos.append(titulo_norm)

                resumo = getattr(entry, "summary", "") or getattr(entry, "description", "")
                resumo = re.sub(r"<[^>]+>", " ", resumo).strip()  # remove HTML solto que alguns feeds trazem no resumo
                link = getattr(entry, "link", "")

                itens.append(NewsItem(
                    fonte=nome_fonte,
                    categoria=categoria,
                    titulo=titulo,
                    resumo=resumo[:400],  # limita tamanho do resumo bruto
                    link=link,
                    publicado=pub,
                ))
        except Exception as e:
            log.warning(f"  -> Erro ao processar itens de {nome_fonte}: {e}")
            continue

    log.info(f"Total de notícias coletadas (sem duplicatas) na janela de {janela_horas}h: {len(itens)}")
    return itens


# ---------------------------------------------------------------------------
# 2. GERAÇÃO DO RELATÓRIO — Gemini (principal) com fallback para Ollama local
# ---------------------------------------------------------------------------
# Existem 3 tipos de relatório, cada um com seu próprio prompt e janela de
# tempo de notícias — pensados pra rodar em horários diferentes do dia.

REGRAS_COMUNS_FORMATO = """\
Formate a resposta em HTML compatível com o Telegram (parse_mode=HTML). Use SOMENTE estas tags, \
sempre abertas e fechadas corretamente, sem aninhar tags iguais: <b>negrito</b>, <i>itálico</i> e \
<a href="URL">link</a>. NÃO use <br>, <ul>, <li>, <h1> a <h6>, <p>, nem markdown (nada de **, ##, \
- ou *). Para listas, use o caractere • no início da linha e uma quebra de linha normal entre itens.

REGRAS IMPORTANTES:
- Nunca invente fatos, números ou eventos que não estejam nas notícias fornecidas.
- NÃO dê recomendação de compra/venda nem previsão de preço — apenas contexto e viés qualitativo \
  (ex: "tende a pressionar" / "pode dar suporte").
- Se uma notícia parecer boato ou sem fonte confiável, sinalize isso no próprio bullet.
- Seja direto e objetivo.
- Termine com uma linha curta em <i>itálico</i> avisando que isso é apoio informativo, não \
  recomendação de investimento.
- Escreva SOMENTE o relatório final, sem comentários sobre o seu processo, sem markdown, e com as \
  tags HTML sempre corretamente fechadas (nunca deixe uma tag aberta sem fechar).
"""

PROMPT_SISTEMA_ABERTURA = """\
Você é um analista de mercado especializado em ajudar um operador de day trade brasileiro que \
opera WIN (mini contrato futuro de Ibovespa) e, secundariamente, WDO/DOL (mini contrato futuro \
de dólar).

Você vai receber uma lista bruta de manchetes de notícias das últimas horas, com fonte, categoria, \
horário e um pequeno resumo de cada uma. Produza um RELATÓRIO DIÁRIO DE ABERTURA (pré-pregão), \
em português.

""" + REGRAS_COMUNS_FORMATO + """
Estrutura OBRIGATÓRIA, sempre nesta ordem, com estes títulos exatos em negrito:

<b>🎯 Resumo do Dia — {data}</b>
2 a 4 linhas com a visão geral do sentimento de mercado hoje: diga explicitamente se o tom geral \
é Risk-On, Risk-Off ou misto, e por quê.

<b>📊 Impacto Direto no WIN (Ibovespa Futuro)</b>
Bullets (•) com o que pode mexer o Ibovespa/WIN hoje: commodities, Vale, Petrobras, Eletrobras, \
bancos, balanços corporativos, curva de juros (DI). Para cada item, diga o fato e o viés (alta, \
baixa ou incerto) para o índice.

<b>💵 Câmbio e Exterior / WDO</b>
Bullets sobre dólar, falas de dirigentes do Fed (Fedspeak), Treasuries americanos, dados de \
emprego e inflação dos EUA (Payroll/CPI) e China — tudo que afeta o câmbio e o fluxo para a bolsa \
brasileira.

<b>🏛️ Política e Fiscal Brasil</b>
Bullets sobre Congresso, pauta fiscal, LDO/orçamento, e indicadores como IPCA e Boletim Focus, com \
potencial de afetar a percepção de risco fiscal/institucional.

<b>📅 Fique de Olho</b>
Lista dos eventos e divulgações do próprio dia (ou dos próximos dias) mencionados nas notícias, \
com horário quando disponível (ex: "10h00 — IPCA-15 (IBGE)"). Se nenhuma fonte trouxer horário, \
liste os eventos sem horário.

Se não houver notícia relevante para alguma seção, escreva "Sem destaques hoje." nessa seção.
"""

PROMPT_SISTEMA_ATUALIZACAO = """\
Você é um analista de mercado brasileiro focado em WIN (Ibovespa futuro) e WDO/DOL (dólar futuro). \
Você já enviou um relatório mais cedo hoje (pode vir reproduzido abaixo, como contexto). Agora \
você recebeu notícias novas, publicadas nas últimas horas.

Sua tarefa é escrever um BOLETIM DE ATUALIZAÇÃO curto — só o que é realmente NOVO e relevante \
desde o relatório anterior. NÃO repita o que já foi dito lá, a menos que uma notícia nova mude o \
quadro (nesse caso, deixe claro o que mudou).

""" + REGRAS_COMUNS_FORMATO + """
Estrutura:

<b>🔄 Atualização — {hora}</b>
2 a 4 bullets (•) com o que há de novo e por que importa para WIN/WDO agora. Se alguma notícia \
nova mudar o viés geral (ex.: de Risk-Off para Risk-On), destaque isso na primeira linha, em \
negrito.

Se não houver nada relevante e genuinamente novo desde o relatório anterior, escreva SOMENTE:
<b>🔄 Atualização — {hora}</b>
Sem novidades relevantes desde o último relatório.
"""

PROMPT_SISTEMA_FECHAMENTO = """\
Você é um analista de mercado brasileiro focado em WIN (Ibovespa futuro) e WDO/DOL (dólar futuro). \
Agora é o fim do pregão. Com base nas notícias do dia (e no relatório da manhã, se vier \
reproduzido abaixo como contexto), escreva um RESUMO DE FECHAMENTO em português.

""" + REGRAS_COMUNS_FORMATO + """
Estrutura OBRIGATÓRIA, sempre nesta ordem, com estes títulos exatos em negrito:

<b>🔔 Fechamento do Pregão — {data}</b>
3 a 5 linhas sobre como o dia terminou (Ibovespa/WIN, Dólar/WDO, juros) — SOMENTE com números ou \
direções que estejam de fato nas notícias recebidas. Se as notícias não trouxerem o fechamento \
numérico exato, descreva qualitativamente (ex: "recuou", "fechou perto da estabilidade") em vez \
de inventar um número.

<b>📌 O que moveu o pregão hoje</b>
Bullets (•) com os 3 a 5 fatores que mais pesaram no dia, comparando com o que foi antecipado no \
relatório da manhã quando possível (confirmou a expectativa? mudou o cenário?).

<b>🌎 De olho no overnight/exterior</b>
Bullets com o que já se sabe sobre o after-market americano, Ásia, ou eventos que podem \
influenciar a abertura do pregão de amanhã.

Se não houver notícia relevante para alguma seção, escreva "Sem destaques hoje." nessa seção.
"""

PROMPT_SISTEMA_POS_NY = """\
Você é um analista de mercado brasileiro focado em WIN (Ibovespa futuro) e WDO/DOL (dólar futuro). \
A B3 já abriu (10h00) e Wall Street acabou de abrir (10h30). Você já enviou relatórios mais cedo \
hoje (podem vir reproduzidos abaixo, como contexto — não repita o que já foi dito, só o que é \
novo ou confirma/muda o cenário). Com base nas notícias mais recentes, escreva uma leitura de \
PÓS-ABERTURA DE NOVA YORK / TENDÊNCIA DO PREGÃO.

""" + REGRAS_COMUNS_FORMATO + """
Estrutura OBRIGATÓRIA, sempre nesta ordem, com estes títulos exatos em negrito:

<b>🗽 Abertura de Nova York — {hora}</b>
2 a 3 linhas sobre como os principais índices americanos (Dow Jones, S&P 500, Nasdaq) abriram e \
reagiram nos primeiros minutos, com base no que estiver nas notícias.

<b>📈 Vale, Petrobras e Bancos na B3</b>
Bullets (•) sobre como essas ações e o setor bancário estão reagindo desde a abertura da B3, \
cruzando com o desempenho de commodities e do exterior.

<b>🎯 Tendência do WIN para a manhã</b>
2 a 3 linhas consolidando um viés de tendência (alta, baixa, lateral/indefinido) para o restante \
da manhã, com base em tudo que já se sabe até agora — contexto qualitativo, nunca previsão de \
preço.

Se não houver notícia relevante para alguma seção, escreva "Sem destaques até o momento." nessa \
seção.
"""

TIPOS_RELATORIO = {
    "abertura": {"prompt": PROMPT_SISTEMA_ABERTURA, "janela_horas": 24},
    "atualizacao": {"prompt": PROMPT_SISTEMA_ATUALIZACAO, "janela_horas": 3},
    "pos_ny": {"prompt": PROMPT_SISTEMA_POS_NY, "janela_horas": 2},
    "fechamento": {"prompt": PROMPT_SISTEMA_FECHAMENTO, "janela_horas": 15},
}

# ---------------------------------------------------------------------------
# 2b. BREAKING NEWS — monitoramento em tempo real, fora da grade de horários
# ---------------------------------------------------------------------------
# Roda como um tipo à parte (não usa TIPOS_RELATORIO/gerar_relatorio): em vez de
# montar UM relatório agregando várias notícias, verifica notícias muito recentes
# uma a uma e dispara um alerta imediato pra cada uma que bater nas palavras-chave
# de alto impacto e ainda não tiver sido alertada.

PROMPT_SISTEMA_BREAKING = """\
Você é um analista de mercado brasileiro. Você vai receber o título, resumo e fonte de UMA única \
notícia que acabou de ser publicada e bateu em palavras-chave de alto impacto para quem opera WIN \
(Ibovespa futuro) e WDO/DOL (dólar futuro).

Escreva um alerta ultracurto em português, em HTML compatível com o Telegram. Use SOMENTE <b> e \
<i> (nada de <a>, <br>, <ul>, markdown, etc). Siga EXATAMENTE este formato, sem título nem \
saudação, só estas duas linhas:

<b>Resumo:</b> até 2 frases curtas e diretas explicando o que aconteceu e por que importa agora \
pro WIN/WDO — direto ao ponto, sem floreio.
<b>Viés:</b> ALTA, BAIXA ou INCERTO para o WIN (mencione o WDO também se fizer sentido).

Regras:
- Não invente nenhum fato além do que está no título/resumo fornecido.
- Se o texto não permitir concluir um viés claro, use INCERTO — não force uma direção.
- Não dê recomendação de compra/venda nem previsão de preço.
- Seja extremamente direto: isso é um alerta urgente, não um relatório.
"""

PROMPT_FILTRO_OLLAMA = """\
Você é um analista de mercado. A seguir há uma lista bruta de manchetes de notícias das últimas \
horas.

Selecione APENAS as 15 notícias mais relevantes para quem opera WIN (Ibovespa futuro) e WDO \
(dólar futuro) hoje. Critérios de relevância: juros/Copom/Fed, inflação (IPCA/CPI), fiscal Brasil, \
Vale/Petrobras/bancos/Eletrobras, câmbio, commodities, geopolítica com impacto em mercado, \
eleições/crise institucional.

Responda SOMENTE repetindo, sem alterar uma palavra, o texto original (fonte, categoria, horário, \
título e resumo) de cada uma das até 15 notícias selecionadas — uma por bloco, na mesma formatação \
em que foram recebidas. Não acrescente comentários, numeração extra nem explicações.
"""


def _ler_ultimo_relatorio_do_dia(data_hoje: str, excluir_tipo: str) -> str | None:
    """Procura o relatório mais recente já gerado hoje (de qualquer outro tipo), pra usar
    como contexto e evitar que o próximo relatório repita o que já foi dito."""
    candidatos = []
    for tipo_nome in TIPOS_RELATORIO:
        if tipo_nome == excluir_tipo:
            continue
        caminho = f"relatorio_{tipo_nome}_{data_hoje}.txt"
        if os.path.exists(caminho):
            candidatos.append((os.path.getmtime(caminho), caminho))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    caminho_mais_recente = candidatos[0][1]
    with open(caminho_mais_recente, "r", encoding="utf-8") as f:
        return f.read()


def montar_bloco_noticias(itens: list[NewsItem]) -> str:
    """Formata a lista de notícias cruas em texto para enviar à IA."""
    linhas = []
    for it in itens:
        hora = it.publicado.strftime("%d/%m %H:%M UTC")
        linhas.append(
            f"- [{it.categoria}] ({it.fonte}, {hora}) {it.titulo}\n"
            f"  Resumo: {it.resumo}\n  Link: {it.link}"
        )
    return "\n".join(linhas)


def _chamar_gemini(system_prompt: str, user_prompt: str) -> str:
    """Chama a API gratuita do Google Gemini. Levanta exceção se algo der errado
    (sem internet, chave inválida/ausente, cota estourada, resposta vazia, etc.)."""
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "COLE_SUA_CHAVE_AQUI":
        raise RuntimeError("GEMINI_API_KEY não configurada em config.py.")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    resposta = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.3,
        ),
    )
    texto = (getattr(resposta, "text", None) or "").strip()
    if not texto:
        raise RuntimeError("Gemini respondeu vazio (pode ser bloqueio de segurança ou cota esgotada).")
    return texto


def _chamar_ollama_chat(system_prompt: str, user_prompt: str, timeout: int = 600) -> str:
    """Chama o Ollama local via API de chat. Levanta exceção se o Ollama não estiver rodando
    ou responder vazio."""
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    try:
        resp = requests.post(f"{config.OLLAMA_URL}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Não consegui me conectar ao Ollama. Verifique se o programa Ollama está "
            "aberto/rodando no seu computador (veja o README.md)."
        )

    dados = resp.json()
    texto = dados.get("message", {}).get("content", "").strip()
    if not texto:
        raise RuntimeError("O Ollama respondeu vazio. Tente rodar de novo ou trocar o modelo em config.py.")
    return texto


def _filtrar_com_ollama(itens: list[NewsItem]) -> list[NewsItem]:
    """ETAPA 1 do fallback (map-reduce): pede ao Ollama pra listar só as 15 notícias mais
    importantes, pra não estourar o contexto do modelo local na etapa seguinte."""
    bloco = montar_bloco_noticias(itens)
    resposta = _chamar_ollama_chat(PROMPT_FILTRO_OLLAMA, bloco, timeout=300)

    selecionados = [it for it in itens if it.titulo[:40].lower() in resposta.lower()]
    if not selecionados:
        # Rede de segurança: se o modelo reformulou o texto e não deu pra casar os títulos,
        # usa as 15 notícias mais recentes em vez de falhar o relatório inteiro.
        log.warning("Não consegui casar as notícias filtradas pelo Ollama com a lista original; usando as 15 mais recentes.")
        selecionados = sorted(itens, key=lambda x: x.publicado, reverse=True)[:15]
    return selecionados[:15]


# ---------------------------------------------------------------------------
# Funções de apoio do Breaking News
# ---------------------------------------------------------------------------

BREAKING_STATE_FILE = "breaking_news_enviadas.json"
BREAKING_STATE_MAX_ITENS = 1000  # limite pra não deixar o arquivo crescer sem parar
BREAKING_MAX_ALERTAS_POR_EXECUCAO = 5  # trava de segurança contra um pico anormal de notícias


def _normalizar_para_busca(texto: str) -> str:
    """Minúsculo e sem acento, pra comparar palavra-chave sem depender de acentuação."""
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.lower()


def _bate_palavra_chave_breaking(item: NewsItem) -> bool:
    texto = _normalizar_para_busca(f"{item.titulo} {item.resumo}")
    return any(_normalizar_para_busca(palavra) in texto for palavra in config.PALAVRAS_CHAVE_BREAKING)


def _chave_breaking(item: NewsItem) -> str:
    """Identificador único da notícia pra controle de 'já alertada' — usa o link quando existe,
    senão cai pro título normalizado."""
    return item.link.strip() if item.link else _titulo_normalizado(item.titulo)


def _carregar_breaking_enviadas() -> set[str]:
    if not os.path.exists(BREAKING_STATE_FILE):
        return set()
    try:
        with open(BREAKING_STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception as e:
        log.warning(f"Não consegui ler {BREAKING_STATE_FILE} (tratando como vazio): {e}")
        return set()


def _salvar_breaking_enviadas(chaves: set[str]):
    lista = list(chaves)[-BREAKING_STATE_MAX_ITENS:]
    with open(BREAKING_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(lista, f, ensure_ascii=False)


def _escapar_html(texto: str) -> str:
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _dentro_da_janela_ativa_breaking() -> bool:
    """Só vale a pena checar breaking news dentro do horário de mercado (evita gastar cota da
    API e bater nos feeds de madrugada, sem necessidade)."""
    agora_hhmm = _agora().strftime("%H:%M")
    return config.BREAKING_HORA_INICIO <= agora_hhmm <= config.BREAKING_HORA_FIM


def _gerar_alerta_breaking(item: NewsItem) -> str:
    """Chama a IA (Gemini, com fallback pro Ollama) só com essa notícia, pra gerar um
    resumo ultracurto de impacto imediato."""
    user_prompt = (
        f"Fonte: {item.fonte}\n"
        f"Título: {item.titulo}\n"
        f"Resumo: {item.resumo}\n"
    )
    try:
        corpo = _chamar_gemini(PROMPT_SISTEMA_BREAKING, user_prompt)
    except Exception as e:
        log.warning(f"Gemini falhou no alerta breaking ({e}); tentando Ollama...")
        corpo = _chamar_ollama_chat(PROMPT_SISTEMA_BREAKING, user_prompt, timeout=90)

    hora = _agora().strftime("%H:%M")
    linha_fonte = f'Fonte: {item.fonte}'
    if item.link:
        linha_fonte += f' — <a href="{item.link}">ver notícia completa</a>'

    return (
        f"<b>🚨 BREAKING NEWS — {hora}</b>\n"
        f"<b>{_escapar_html(item.titulo)}</b>\n\n"
        f"{corpo}\n\n"
        f"{linha_fonte}"
    )


def verificar_breaking_news():
    """Ponto de entrada do modo 'breaking': verifica notícias muito recentes, filtra por
    palavra-chave de alto impacto, e dispara um alerta imediato pra cada uma que ainda não
    tiver sido enviada. Feito pra ser chamado a cada poucos minutos (ver README.md)."""
    if not _dentro_da_janela_ativa_breaking():
        log.info(f"Fora da janela ativa de breaking news ({config.BREAKING_HORA_INICIO}-{config.BREAKING_HORA_FIM}), pulando.")
        return

    log.info(f"Verificando breaking news (últimos {config.BREAKING_JANELA_MINUTOS} min)...")
    itens = coletar_noticias(janela_horas=config.BREAKING_JANELA_MINUTOS / 60)
    candidatos = [it for it in itens if _bate_palavra_chave_breaking(it)]

    if not candidatos:
        log.info("Nenhuma notícia bateu nas palavras-chave de breaking news.")
        return

    ja_enviadas = _carregar_breaking_enviadas()
    novas = [it for it in candidatos if _chave_breaking(it) not in ja_enviadas]

    if not novas:
        log.info(f"{len(candidatos)} notícia(s) bateram nas palavras-chave, mas já tinham sido alertadas.")
        return

    if len(novas) > BREAKING_MAX_ALERTAS_POR_EXECUCAO:
        log.warning(f"{len(novas)} notícias novas de uma vez — limitando a {BREAKING_MAX_ALERTAS_POR_EXECUCAO} pra não disparar demais.")
        novas = novas[:BREAKING_MAX_ALERTAS_POR_EXECUCAO]

    log.info(f"{len(novas)} breaking news nova(s) encontrada(s). Gerando e enviando alertas...")

    for item in novas:
        chave = _chave_breaking(item)
        try:
            texto_alerta = _gerar_alerta_breaking(item)
        except Exception as e:
            log.error(f"Falha ao gerar alerta pra '{item.titulo[:60]}': {e}")
            continue

        try:
            if config.ENVIAR_TELEGRAM:
                enviar_telegram(texto_alerta)
            else:
                log.info("Envio via Telegram desativado — alerta gerado mas não enviado.")
            ja_enviadas.add(chave)
            _salvar_breaking_enviadas(ja_enviadas)
        except Exception as e:
            log.error(f"Falha ao enviar alerta pro Telegram ('{item.titulo[:60]}'): {e}")


def _montar_mensagem_usuario(bloco_noticias: str, data_hoje: str, hora_agora: str, relatorio_anterior: str | None) -> str:
    partes = [f"Data de hoje: {data_hoje}", f"Hora atual: {hora_agora}"]
    if relatorio_anterior:
        partes.append(
            "Relatório enviado mais cedo hoje (use só como contexto, não repita o que já foi dito):"
            f"\n\n{relatorio_anterior}"
        )
    partes.append(f"Lista de notícias coletadas agora (bruta, pode conter ruído):\n\n{bloco_noticias}")
    return "\n\n".join(partes)


def gerar_relatorio(itens: list[NewsItem], tipo: str = "abertura", relatorio_anterior: str | None = None) -> str:
    """Gera o relatório final do tipo pedido (abertura, atualizacao ou fechamento). Tenta o
    Gemini primeiro (contexto grande, não precisa reduzir a lista de notícias); se falhar, cai
    para o Ollama local em 2 etapas."""
    data_hoje = _agora().strftime("%d/%m/%Y")
    hora_agora = _agora().strftime("%H:%M")

    if not itens and not relatorio_anterior:
        return f"<b>Relatório ({tipo})</b>\n\nNenhuma notícia coletada na janela de tempo. Verifique os feeds RSS."

    prompt_base = TIPOS_RELATORIO[tipo]["prompt"]
    system_prompt = prompt_base.replace("{data}", data_hoje).replace("{hora}", hora_agora)

    bloco_completo = montar_bloco_noticias(itens)
    user_prompt = _montar_mensagem_usuario(bloco_completo, data_hoje, hora_agora, relatorio_anterior)

    try:
        log.info(f"Chamando Gemini ({config.GEMINI_MODEL})...")
        return _chamar_gemini(system_prompt, user_prompt)
    except Exception as e:
        log.warning(f"Gemini falhou ({e}). Caindo para o Ollama local como fallback...")

    try:
        if len(itens) > 15:
            log.info("ETAPA 1/2 (Ollama): filtrando as 15 notícias mais relevantes...")
            itens_reduzidos = _filtrar_com_ollama(itens)
        else:
            itens_reduzidos = itens

        log.info(f"ETAPA 2/2 (Ollama): gerando relatório com {len(itens_reduzidos)} notícias...")
        bloco_filtrado = montar_bloco_noticias(itens_reduzidos)
        user_prompt_filtrado = _montar_mensagem_usuario(bloco_filtrado, data_hoje, hora_agora, relatorio_anterior)
        return _chamar_ollama_chat(system_prompt, user_prompt_filtrado, timeout=600)
    except Exception as e:
        raise RuntimeError(f"Tanto o Gemini quanto o fallback do Ollama falharam. Último erro: {e}")


# ---------------------------------------------------------------------------
# 3a. ENVIO VIA TELEGRAM
# ---------------------------------------------------------------------------

TELEGRAM_LIMITE_CARACTERES = 4000  # Telegram corta em 4096; usamos margem de segurança


def _dividir_em_blocos(texto: str, limite: int = TELEGRAM_LIMITE_CARACTERES) -> list[str]:
    """Divide o texto em blocos que cabem no limite de caracteres do Telegram,
    tentando quebrar em parágrafos para não cortar frases no meio."""
    if len(texto) <= limite:
        return [texto]

    blocos = []
    paragrafos = texto.split("\n")
    atual = ""
    for par in paragrafos:
        candidato = f"{atual}\n{par}" if atual else par
        if len(candidato) > limite:
            if atual:
                blocos.append(atual)
            # parágrafo sozinho já é maior que o limite: corta na força bruta
            if len(par) > limite:
                for i in range(0, len(par), limite):
                    blocos.append(par[i:i + limite])
                atual = ""
            else:
                atual = par
        else:
            atual = candidato
    if atual:
        blocos.append(atual)
    return blocos


def enviar_telegram(texto_html: str):
    """Envia o relatório para o Telegram (formatado em HTML), dividindo em múltiplas
    mensagens se for muito longo. Se o HTML vier malformado, reenvia como texto puro."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não configurados em config.py. "
            "Veja o README.md para o passo a passo."
        )

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    blocos = _dividir_em_blocos(texto_html)

    for i, bloco in enumerate(blocos, start=1):
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": bloco,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, data=payload, timeout=30)
        if resp.status_code != 200:
            # Se der erro de parsing de HTML (tag quebrada), reenvia como texto puro sem as tags
            log.warning(f"Falha ao enviar bloco {i} formatado, tentando texto puro: {resp.text}")
            texto_puro = re.sub(r"<[^>]+>", "", bloco)
            resp = requests.post(url, data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": texto_puro,
                "disable_web_page_preview": True,
            }, timeout=30)
            resp.raise_for_status()

    log.info(f"Relatório enviado ao Telegram em {len(blocos)} mensagem(ns).")


# ---------------------------------------------------------------------------
# 3b. ENVIO POR E-MAIL
# ---------------------------------------------------------------------------

def enviar_email(assunto: str, corpo_relatorio: str):
    """Envia o relatório por e-mail via SMTP (configurado em config.py)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = config.EMAIL_REMETENTE
    msg["To"] = config.EMAIL_DESTINATARIO

    texto_plano = re.sub(r"<[^>]+>", "", corpo_relatorio)
    parte_texto = MIMEText(texto_plano, "plain", "utf-8")

    corpo_html = corpo_relatorio.replace("\n", "<br>\n")
    corpo_html = f"<div style='font-family: sans-serif; white-space: pre-wrap;'>{corpo_html}</div>"
    parte_html = MIMEText(corpo_html, "html", "utf-8")

    msg.attach(parte_texto)
    msg.attach(parte_html)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as servidor:
        servidor.starttls()
        servidor.login(config.EMAIL_REMETENTE, config.EMAIL_SENHA)
        servidor.sendmail(config.EMAIL_REMETENTE, config.EMAIL_DESTINATARIO, msg.as_string())

    log.info(f"E-mail enviado para {config.EMAIL_DESTINATARIO}")


# ---------------------------------------------------------------------------
# 4. MAIN
# ---------------------------------------------------------------------------

TITULOS_TIPO = {
    "abertura": "Relatório de Abertura",
    "atualizacao": "Boletim de Atualização",
    "pos_ny": "Pós-Abertura de Nova York",
    "fechamento": "Resumo de Fechamento",
}

TIPOS_VALIDOS_CLI = list(TIPOS_RELATORIO) + ["breaking"]


def _validar_variaveis_obrigatorias():
    """Confere as variáveis essenciais ANTES de gastar tempo coletando notícias. Se faltar
    algo, avisa exatamente qual chave está ausente e para na hora, em vez de falhar no meio
    da execução com um erro mais difícil de entender."""
    faltando = []
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "COLE_SUA_CHAVE_AQUI":
        faltando.append("GEMINI_API_KEY")
    if config.ENVIAR_TELEGRAM:
        if not config.TELEGRAM_BOT_TOKEN:
            faltando.append("TELEGRAM_BOT_TOKEN")
        if not config.TELEGRAM_CHAT_ID:
            faltando.append("TELEGRAM_CHAT_ID")

    if faltando:
        print(
            "ERRO DE CONFIGURACAO: faltando a(s) variavel(is): " + ", ".join(faltando) + "\n"
            "Se estiver rodando local: confira o config.py.\n"
            "Se estiver rodando no GitHub Actions: confira Settings > Secrets and variables > "
            "Actions no repositorio, e cadastre um Secret com esse nome exato pra cada item "
            "listado acima."
        )
        log.error("Variável(is) obrigatória(s) ausente(s): " + ", ".join(faltando))
        sys.exit(1)


def main():
    _validar_variaveis_obrigatorias()

    tipo = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "abertura"

    if tipo == "breaking":
        log.info("=== Verificação de Breaking News ===")
        try:
            verificar_breaking_news()
        except Exception as e:
            log.error(f"Falha na verificação de breaking news: {e}")
            sys.exit(1)
        log.info("=== Concluído ===")
        return

    if tipo not in TIPOS_RELATORIO:
        log.error(f"Tipo de relatório inválido: '{tipo}'. Use um destes: {', '.join(TIPOS_VALIDOS_CLI)}.")
        sys.exit(1)

    log.info(f"=== Iniciando geração do relatório: {tipo} ===")

    data_hoje = _agora().strftime("%Y-%m-%d")
    janela_horas = TIPOS_RELATORIO[tipo]["janela_horas"]

    itens = coletar_noticias(janela_horas=janela_horas)

    relatorio_anterior = None
    if tipo in ("atualizacao", "pos_ny", "fechamento"):
        relatorio_anterior = _ler_ultimo_relatorio_do_dia(data_hoje, excluir_tipo=tipo)

    relatorio = gerar_relatorio(itens, tipo=tipo, relatorio_anterior=relatorio_anterior)

    caminho_saida = f"relatorio_{tipo}_{data_hoje}.txt"
    with open(caminho_saida, "w", encoding="utf-8") as f:
        f.write(relatorio)
    log.info(f"Relatório salvo localmente em: {caminho_saida}")

    algum_envio_falhou = False

    if config.ENVIAR_TELEGRAM:
        try:
            enviar_telegram(relatorio)
        except Exception as e:
            log.error(f"Falha ao enviar Telegram: {e}")
            algum_envio_falhou = True
    else:
        log.info("Envio via Telegram desativado (ENVIAR_TELEGRAM=False em config.py).")

    if config.ENVIAR_EMAIL:
        assunto = f"📊 {TITULOS_TIPO[tipo]} Trading (WIN) — {_agora().strftime('%d/%m/%Y %H:%M')}"
        try:
            enviar_email(assunto, relatorio)
        except Exception as e:
            log.error(f"Falha ao enviar e-mail: {e}")
            algum_envio_falhou = True
    else:
        log.info("Envio de e-mail desativado (ENVIAR_EMAIL=False em config.py).")

    if not config.ENVIAR_TELEGRAM and not config.ENVIAR_EMAIL:
        log.info("Nenhum canal de envio ativado — o relatório só foi salvo localmente.")

    if algum_envio_falhou:
        sys.exit(1)

    log.info("=== Concluído ===")


if __name__ == "__main__":
    main()
