import os
import json
import time
import traceback
import base64
from datetime import datetime, timezone
from typing import Literal, Optional, Any, Dict

from PIL import Image
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()  # Carrega variáveis do arquivo .env (ex: GEMINI_API_KEY)

import requests
import instaloader
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from instaloader.exceptions import (
    BadCredentialsException,
    ConnectionException,
    LoginRequiredException,
    PrivateProfileNotFollowedException,
    QueryReturnedBadRequestException,
    TwoFactorAuthRequiredException,
)

app = FastAPI(title="Instagram Miner API", version="1.2.0")

# Tipos válidos de ordenação dos posts
SortMode = Literal["recent", "most_liked", "most_commented", "best_engagement"]


# Utilitário de erro padronizado 
#  Garante que todos os erros da API retornem sempre no mesmo formato JSON.
def http_error(status_code: int, msg: str, e: Exception, debug: bool):
    detail = {
        "message": msg,
        "error_type": type(e).__name__,
        "error": str(e),
    }
    if debug:
        detail["stack"] = traceback.format_exc()
    raise HTTPException(status_code=status_code, detail=detail)


#Configuração do Instaloader 
#  Inicializa o scraper do Instagram. Se um usuário de sessão for informado,
#  carrega o login salvo para evitar bloqueios do Instagram (403/429).
def build_loader(session_user: Optional[str] = None, debug: bool = False) -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    if session_user:
        try:
            L.load_session_from_file(session_user)
        except Exception as e:
            http_error(400, f"Falha ao carregar sessão '{session_user}'.", e, debug)

    return L


# Coleta de dados do Instagram 
#  Função principal de scraping. Busca o perfil e os posts do usuário,
#  calcula o engagement_rate de cada post e aplica ordenação/filtros.
def mine_profile(
    username: str,
    max_posts: int = 30,
    sleep_s: float = 2.0,
    sort: SortMode = "recent",
    include_caption: bool = True,
    caption_max_len: int = 2000,
    session_user: Optional[str] = None,
    debug: bool = False,
    min_rate: Optional[float] = None,
    top_rate: Optional[int] = None,
) -> dict:
    username = username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username é obrigatório")

    L = build_loader(session_user=session_user, debug=debug)

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except (LoginRequiredException, BadCredentialsException, TwoFactorAuthRequiredException) as e:
        http_error(401, f"Sessão inválida ou login necessário para acessar '{username}'.", e, debug)
    except PrivateProfileNotFollowedException as e:
        http_error(403, f"Perfil '{username}' é privado e a sessão atual não segue o perfil.", e, debug)
    except (ConnectionException, QueryReturnedBadRequestException) as e:
        http_error(502, f"Falha de conexão/consulta ao Instagram ao carregar '{username}'.", e, debug)
    except Exception as e:
        http_error(502, f"Erro inesperado ao carregar perfil '{username}'.", e, debug)

    posts = []
    try:
        for i, post in enumerate(profile.get_posts(), start=1):
            if i > max_posts:
                break

            caption = post.caption or ""
            if not include_caption:
                caption = ""
            else:
                caption = caption[:caption_max_len]

            posts.append(
                {
                    "shortcode": post.shortcode,
                    "url": f"https://www.instagram.com/p/{post.shortcode}/",
                    "date_utc": post.date_utc.replace(tzinfo=timezone.utc).isoformat(),
                    "likes": int(post.likes),
                    "comments": int(post.comments),
                    "caption": caption,
                    "typename": post.typename,
                    "is_video": bool(post.is_video),
                }
            )

            time.sleep(max(0.0, sleep_s))

    except (LoginRequiredException,) as e:
        http_error(401, f"Login necessário durante a varredura de posts de '{username}'.", e, debug)
    except PrivateProfileNotFollowedException as e:
        http_error(403, f"Perfil '{username}' é privado e não pode ser varrido pela sessão atual.", e, debug)
    except (ConnectionException, QueryReturnedBadRequestException) as e:
        http_error(502, f"Falha do Instagram durante varredura de posts de '{username}'.", e, debug)
    except Exception as e:
        http_error(502, f"Erro inesperado ao varrer posts de '{username}'.", e, debug)

    followers = profile.followers or 0
    for p in posts:
        if followers > 0:
            p["engagement_rate"] = (p["likes"] + p["comments"]) / followers
        else:
            p["engagement_rate"] = None

    if sort == "recent":
        posts.sort(key=lambda x: x["date_utc"], reverse=True)
    elif sort == "most_liked":
        posts.sort(key=lambda x: x["likes"], reverse=True)
    elif sort == "most_commented":
        posts.sort(key=lambda x: x["comments"], reverse=True)
    elif sort == "best_engagement":
        posts.sort(key=lambda x: (x["engagement_rate"] is not None, x["engagement_rate"]), reverse=True)

    if (min_rate is not None) or (top_rate is not None):
        posts_by_rate = sorted(
            posts,
            key=lambda x: (x["engagement_rate"] is not None, x["engagement_rate"]),
            reverse=True,
        )

        if min_rate is not None:
            posts_by_rate = [
                p for p in posts_by_rate
                if p["engagement_rate"] is not None and p["engagement_rate"] >= min_rate
            ]

        if top_rate is not None:
            posts_by_rate = posts_by_rate[:top_rate]

        posts = posts_by_rate

    out = {
        "profile": {
            "username": profile.username,
            "full_name": profile.full_name,
            "biography": profile.biography,
            "external_url": profile.external_url,
            "followers": int(profile.followers),
            "followees": int(profile.followees),
            "mediacount": int(profile.mediacount),
            "is_verified": bool(profile.is_verified),
            "is_private": bool(profile.is_private),
        },
        "params": {
            "max_posts": max_posts,
            "sleep_s": sleep_s,
            "sort": sort,
            "include_caption": include_caption,
            "caption_max_len": caption_max_len,
            "session_user": session_user,
            "debug": debug,
            "min_rate": min_rate,
            "top_rate": top_rate,
        },
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "posts": posts,
    }
    return out



# Funções de apoio usadas pelos endpoints

# Seleciona um post da lista pelo shortcode. Se não informado, retorna o primeiro.
def _pick_post(posts: list, shortcode: Optional[str] = None) -> Optional[dict]:
    if not posts:
        return None
    if shortcode:
        for p in posts:
            if p.get("shortcode") == shortcode:
                return p
        return None
    return posts[0]


# Monta o prompt que será enviado ao Gemini com os dados do post.
def _build_prompt_from_post(post: dict) -> str:
    caption = (post.get("caption") or "").strip()
    return (
        "Você é um social media especialista.\n"
        "Analise o post abaixo e crie:\n"
        "1) sentiment (positivo|neutro|negativo)\n"
        "2) summary (até 3 linhas)\n"
        "3) similar_post_text (até 280 caracteres)\n"
        "4) similar_image_prompt (descrição curta da imagem sugerida)\n\n"
        f"Dados do post:\n"
        f"- url: {post.get('url')}\n"
        f"- likes: {post.get('likes')}\n"
        f"- comments: {post.get('comments')}\n"
        f"- is_video: {post.get('is_video')}\n"
        f"- typename: {post.get('typename')}\n\n"
        f"Caption do post:\n{caption}\n"
    )


# Baixa uma imagem de uma URL pública e converte para base64.
# Retorna uma tupla: (base64, mime_type).
def _download_image_as_base64(url: str, timeout_s: int = 20) -> tuple[str, str]:
    r = requests.get(url, timeout=timeout_s, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not content_type or "image" not in content_type:
        content_type = "image/jpeg"

    b64 = base64.b64encode(r.content).decode("utf-8")
    return b64, content_type


# Remove o prefixo de Data URL (ex: "data:image/png;base64,...") se existir.
# retornando apenas o base64 limpo e o mime detectado.
def _strip_data_url_prefix(image_b64: str) -> tuple[str, Optional[str]]:
    if not image_b64:
        return image_b64, None

    s = image_b64.strip()

    if s.startswith("data:") and ";base64," in s:
        header, b64 = s.split(";base64,", 1)
        mime = header[5:] if header.startswith("data:") else None
        return b64.strip(), (mime.strip() if mime else None)

    return s, None


# Redimensiona a imagem para um tamanho máximo e converte para JPEG.
# Isso reduz o tamanho do payload enviado ao Gemini e padroniza o formato.
def _resize_base64_image(image_b64: str, max_size: int) -> str:
    image_bytes = base64.b64decode(image_b64)
    img = Image.open(BytesIO(image_bytes))

    if img.mode != "RGB":
        img = img.convert("RGB")

    img.thumbnail((max_size, max_size))

    buffer = BytesIO()
    img.save(buffer, format="JPEG", quality=85, optimize=True)

    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# Faz a chamada HTTP para a API do Gemini com retry automático (até 3 tentativas).
def _call_gemini_generate_content(parts: list, model: str) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY não configurada")

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1200,
        },
    }

    last_err = None
    for attempt in range(3):
        try:
            resp = requests.post(endpoint, json=payload, timeout=(20, 180))
            if not resp.ok:
                raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text}")
            return resp.json()
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"Gemini request falhou após retries: {last_err}")


# monta(texto + imagem opcional),
# envia ao Gemini e retorna o JSON parsed com resultado + uso de tokens.
def gemini_analyze(caption: str, image_b64: Optional[str], image_mime: str, model: str) -> Dict[str, Any]:
    instruction = (
        "Retorne APENAS um JSON válido, sem markdown e sem texto extra.\n"
        "Campos obrigatórios:\n"
        "sentiment: 'positivo' | 'neutro' | 'negativo'\n"
        "summary: string (até 3 linhas)\n"
        "similar_post_text: string (até 280 caracteres)\n"
        "similar_image_prompt: string (descrição curta da imagem sugerida)\n"
    )

    parts = [{"text": f"{instruction}\n\nCaption:\n{caption}"}]

    if image_b64:
        parts.append(
            {
                "inline_data": {
                    "mime_type": image_mime,
                    "data": image_b64,
                }
            }
        )

    data = _call_gemini_generate_content(parts=parts, model=model)

    usage_md = data.get("usageMetadata") or {}
    prompt_tokens = usage_md.get("promptTokenCount")
    output_tokens = usage_md.get("candidatesTokenCount")
    total_tokens = usage_md.get("totalTokenCount")

    candidates = data.get("candidates") or []
    raw_text = ""

    if candidates:
        content = candidates[0].get("content") or {}
        out_parts = content.get("parts") or []

        texts = []
        for part in out_parts:
            if isinstance(part, dict) and "text" in part:
                texts.append(part["text"] or "")

        raw_text = "".join(texts).strip()

    try:
        cleaned = (raw_text or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned) if cleaned else {}
    except Exception:
        parsed = {"raw": raw_text}

    return {
        "result": parsed,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "model": model,
    }


#  ENDPOINTS — Rotas disponíveis na API

# Verificação de saúde — confirma que a API está no ar.
@app.get("/health")
def health():
    return {"ok": True}


# Endpoint de scraping puro: retorna perfil + lista de posts com métricas.
@app.get("/v1/instagram/profile")
def api_profile(
    username: str = Query(..., description="Perfil do Instagram. Pode vir com ou sem @"),
    max_posts: int = Query(30, ge=1, le=200, description="Quantos posts coletar (limite de segurança)"),
    sleep_s: float = Query(2.0, ge=0.0, le=10.0, description="Delay entre posts para reduzir bloqueio do Instagram"),
    sort: SortMode = Query("recent", description="Ordenação final dos posts"),
    include_caption: bool = Query(True, description="Incluir legenda no retorno"),
    caption_max_len: int = Query(2000, ge=0, le=10000, description="Limite de caracteres da legenda"),
    session_user: Optional[str] = Query(None, description="Usuário cuja sessão foi salva (load_session_from_file)"),
    debug: bool = Query(False, description="Se true, retorna stacktrace e detalhes completos do erro"),
    min_rate: Optional[float] = Query(None, ge=0.0, le=1.0, description="Filtra posts com engagement_rate >= min_rate"),
    top_rate: Optional[int] = Query(None, ge=1, le=200, description="Retorna apenas os top N posts por engagement_rate"),
):
    return mine_profile(
        username=username,
        max_posts=max_posts,
        sleep_s=sleep_s,
        sort=sort,
        include_caption=include_caption,
        caption_max_len=caption_max_len,
        session_user=session_user,
        debug=debug,
        min_rate=min_rate,
        top_rate=top_rate,
    )


# Modelo de entrada para o endpoint de análise com Gemini.
# Aceita texto (caption), imagem por URL ou base64.
class GeminiAnalyzeRequest(BaseModel):
    caption: str = Field(default="", description="Texto do post")
    image_url: Optional[str] = Field(default=None, description="URL de uma imagem pública do post")
    image_base64: Optional[str] = Field(default=None, description="Imagem em base64 (alternativa ao image_url)")
    image_mime: str = Field(default="image/jpeg", description="Mime type do base64 (image/jpeg, image/png, etc)")
    debug: bool = Field(default=False, description="Se true, retorna detalhes do erro")


# Endpoint de análise isolada: recebe caption + imagem e retorna análise do Gemini.
# Suporta imagem via URL pública ou base64 direto no body.
@app.post("/v1/ai/gemini/analyze")
def api_gemini_analyze(req: GeminiAnalyzeRequest):
    try:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

        image_b64 = None
        image_mime = req.image_mime or "image/jpeg"

        #1) base64 vindo do cliente
        if req.image_base64:
            cleaned_b64, detected_mime = _strip_data_url_prefix(req.image_base64)
            image_b64 = cleaned_b64
            if detected_mime:
                image_mime = detected_mime

        # 2) URL externa → FIX: desempacota a tupla corretamente
        elif req.image_url:
            try:
                image_b64, image_mime = _download_image_as_base64(req.image_url)
                if req.debug:
                    print("Download OK, mime:", image_mime)
            except Exception as e:
                detail = {
                    "message": "Falha ao baixar image_url",
                    "error": str(e),
                }
                if req.debug:
                    detail["stack"] = traceback.format_exc()
                raise HTTPException(status_code=400, detail=detail)

        # 3) Normalizar imagem → FIX: usa _resize_base64_image (função que existe)
        #    e mantém o base64 original em caso de falha, sem zerar
        if image_b64:
            try:
                image_b64 = _resize_base64_image(image_b64, 1024)
                image_mime = "image/jpeg"
            except Exception as img_e:
                if req.debug:
                    print("Falha ao normalizar imagem:", img_e)
                # mantém o base64 original em vez de descartar a imagem
                image_mime = req.image_mime or "image/jpeg"

        gemini_out = gemini_analyze(
            caption=req.caption or "",
            image_b64=image_b64,
            image_mime=image_mime,
            model=model,
        )

        return {
            "ok": True,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "input": {
                "caption_len": len(req.caption or ""),
                "has_image": bool(image_b64),
                "image_source": "base64" if req.image_base64 else ("url" if req.image_url else None),
                "image_mime": image_mime if image_b64 else None,
            },
            "gemini": gemini_out,
        }

    except HTTPException:
        raise
    except Exception as e:
        detail = {
            "message": "Falha ao executar análise no Gemini",
            "error_type": type(e).__name__,
            "error": str(e),
        }
        if req.debug:
            detail["stack"] = traceback.format_exc()

        raise HTTPException(status_code=502, detail=detail)


# Endpoint completo: faz o scraping do Instagram e já manda o post para o Gemini analisar.
# Tenta baixar a imagem do post para enriquecer a análise.
@app.post("/v1/instagram/analyze")
def api_instagram_analyze(
    username: str = Query(...),
    shortcode: Optional[str] = Query(None),
    max_posts: int = Query(10, ge=1, le=50),
    sleep_s: float = Query(0.5, ge=0.0, le=10.0),
    session_user: Optional[str] = Query(None),
    debug: bool = Query(False),
):
    mined = mine_profile(
        username=username,
        max_posts=max_posts,
        sleep_s=sleep_s,
        sort="recent",
        include_caption=True,
        caption_max_len=2000,
        session_user=session_user,
        debug=debug,
    )

    post = _pick_post(mined.get("posts", []), shortcode=shortcode)
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")

    prompt = _build_prompt_from_post(post)

    # FIX: desempacota a tupla corretamente
    image_b64 = None
    image_mime = "image/jpeg"
    try:
        image_url = f"https://www.instagram.com/p/{post['shortcode']}/media/?size=l"
        image_b64, image_mime = _download_image_as_base64(image_url)
    except Exception as e:
        if debug:
            print("Falha ao baixar imagem:", e)
        image_b64 = None

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gemini_out = gemini_analyze(
        caption=prompt,
        image_b64=image_b64,
        image_mime=image_mime,
        model=model,
    )

    return {
        "ok": True,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": mined.get("profile"),
        "post": post,
        "image_included": bool(image_b64),
        "gemini": gemini_out,
    }


# Benchmark de tokens por post: compara o consumo de tokens do Gemini
# em 3 cenários — só texto, imagem 512px e imagem 768px.
@app.post("/v1/benchmark/tokens/post")
def benchmark_tokens_post(
    username: str = Query(...),
    shortcode: Optional[str] = Query(None),
    max_posts: int = Query(5, ge=1, le=20),
    sleep_s: float = Query(0.5, ge=0.0, le=5.0),
    session_user: Optional[str] = Query(None),
    debug: bool = Query(False),
):
    mined = mine_profile(
        username=username,
        max_posts=max_posts,
        sleep_s=sleep_s,
        sort="recent",
        include_caption=True,
        caption_max_len=2000,
        session_user=session_user,
        debug=debug,
    )

    post = _pick_post(mined.get("posts", []), shortcode=shortcode)
    if not post:
        raise HTTPException(status_code=404, detail="Post não encontrado")

    prompt = _build_prompt_from_post(post)
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # 1) Texto apenas
    text_only = gemini_analyze(
        caption=prompt,
        image_b64=None,
        image_mime="image/jpeg",
        model=model,
    )

    # 2) FIX: desempacota a tupla corretamente
    image_url = f"https://www.instagram.com/p/{post['shortcode']}/media/?size=l"
    image_b64_original, _ = _download_image_as_base64(image_url)

    # 3) Imagem 512px
    image_512 = _resize_base64_image(image_b64_original, 512)
    img_512 = gemini_analyze(
        caption=prompt,
        image_b64=image_512,
        image_mime="image/jpeg",
        model=model,
    )

    # 4) Imagem 768px
    image_768 = _resize_base64_image(image_b64_original, 768)
    img_768 = gemini_analyze(
        caption=prompt,
        image_b64=image_768,
        image_mime="image/jpeg",
        model=model,
    )

    return {
        "ok": True,
        "post": {
            "shortcode": post.get("shortcode"),
            "url": post.get("url"),
        },
        "benchmark_tokens": {
            "text_only": text_only.get("usage"),
            "image_512": img_512.get("usage"),
            "image_768": img_768.get("usage"),
        },
    }


# Benchmark em lote: analisa N posts de um perfil (só texto) e calcula
# a média de tokens consumidos — útil para estimar custo da API.
@app.post("/v1/benchmark/tokens")
def api_benchmark_tokens(
    username: str = Query(...),
    n_posts: int = Query(5, ge=1, le=20),
    sleep_s: float = Query(0.5, ge=0.0, le=10.0),
    session_user: Optional[str] = Query(None),
    debug: bool = Query(False),
):
    mined = mine_profile(
        username=username,
        max_posts=n_posts,
        sleep_s=sleep_s,
        sort="recent",
        include_caption=True,
        caption_max_len=2000,
        session_user=session_user,
        debug=debug,
    )

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    results = []

    for p in mined.get("posts", []):
        prompt = _build_prompt_from_post(p)
        out = gemini_analyze(
            caption=prompt,
            image_b64=None,
            image_mime="image/jpeg",
            model=model,
        )

        usage = out.get("usage") or {}
        results.append(
            {
                "shortcode": p.get("shortcode"),
                "total_tokens": usage.get("total_tokens"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("output_tokens"),
            }
        )

    totals = [r["total_tokens"] for r in results if isinstance(r.get("total_tokens"), int)]
    avg = (sum(totals) / len(totals)) if totals else None

    return {
        "ok": True,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "username": mined["profile"]["username"],
        "model": model,
        "n": len(results),
        "avg_total_tokens": avg,
        "items": results,
    }