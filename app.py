import os
import json
import time
import traceback
import base64
from datetime import datetime, timezone
from typing import Literal, Optional, Any, Dict

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

# Modos de ordenação suportados
SortMode = Literal["recent", "most_liked", "most_commented", "best_engagement"]


def http_error(status_code: int, msg: str, e: Exception, debug: bool):
    """
    Padroniza o retorno de erro.
    - Sempre retorna: message, error_type, error
    - Se debug=true, também retorna stack (para diagnóstico)
    """
    detail = {
        "message": msg,
        "error_type": type(e).__name__,
        "error": str(e),
    }
    if debug:
        detail["stack"] = traceback.format_exc()
    raise HTTPException(status_code=status_code, detail=detail)


def build_loader(session_user: Optional[str] = None, debug: bool = False) -> instaloader.Instaloader:
    """
    Inicializa o Instaloader configurado para não baixar mídia (só metadados).
    Se session_user for informado, tenta carregar uma sessão salva para reduzir bloqueios (403/429).
    """
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


def mine_profile(
    username: str,
    max_posts: int = 30,
    sleep_s: float = 2.0,
    sort: SortMode = "recent",
    include_caption: bool = True,
    caption_max_len: int = 2000,
    session_user: Optional[str] = None,
    debug: bool = False,
    # (rate/performance)
    min_rate: Optional[float] = None,
    top_rate: Optional[int] = None,
) -> dict:
    """
    Coleta dados do perfil + posts e monta um JSON com:
    - perfil
    - parâmetros usados
    - data da coleta
    - lista de posts (com engagement_rate calculado)
    Opcionalmente filtra por rate (engagement_rate).
    """
    username = username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username é obrigatório")

    L = build_loader(session_user=session_user, debug=debug)

    # Carrega o perfil (aqui acontecem boa parte dos 403/429)
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

    # Coleta posts limitado por max_posts e respeita sleep_s entre iterações
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
                    "typename": post.typename,  # GraphImage / GraphVideo / GraphSidecar
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

    # Calcula o rate engagement_rate usando followers atuais
    followers = profile.followers or 0
    for p in posts:
        if followers > 0:
            p["engagement_rate"] = (p["likes"] + p["comments"]) / followers
        else:
            p["engagement_rate"] = None

    # Ordenação (comportamento padrão do endpoint)
    if sort == "recent":
        posts.sort(key=lambda x: x["date_utc"], reverse=True)
    elif sort == "most_liked":
        posts.sort(key=lambda x: x["likes"], reverse=True)
    elif sort == "most_commented":
        posts.sort(key=lambda x: x["comments"], reverse=True)
    elif sort == "best_engagement":
        posts.sort(key=lambda x: (x["engagement_rate"] is not None, x["engagement_rate"]), reverse=True)

    # Filtro por "rate" (após cálculo)
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


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/v1/instagram/profile")
def api_profile(
    username: str = Query(..., description="Perfil do Instagram. Pode vir com ou sem @"),
    max_posts: int = Query(30, ge=1, le=200, description="Quantos posts coletar (limite de segurança)"),
    sleep_s: float = Query(2.0, ge=0.0, le=10.0, description="Delay entre posts para reduzir bloqueio"),
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


# -----------------------------------
# POC Gemini (análise + tokens)
# -----------------------------------

class GeminiAnalyzeRequest(BaseModel):
    caption: str = Field(default="", description="Texto do post")
    image_url: Optional[str] = Field(default=None, description="URL de uma imagem pública do post")
    image_base64: Optional[str] = Field(default=None, description="Imagem em base64 (alternativa ao image_url)")
    image_mime: str = Field(default="image/jpeg", description="Mime type do base64 (image/jpeg, image/png, etc)")
    debug: bool = Field(default=False, description="Se true, retorna detalhes do erro")


def _download_image_as_base64(url: str, timeout_s: int = 20) -> str:
    r = requests.get(url, timeout=timeout_s, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return base64.b64encode(r.content).decode("utf-8")


def _call_gemini_generate_content(parts: list, model: str) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY não configurada")

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 700,
        },
    }

    resp = requests.post(endpoint, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()


def gemini_analyze(caption: str, image_b64: Optional[str], image_mime: str, model: str) -> Dict[str, Any]:
    """
    Retorna um JSON com:
    - sentiment (positivo/neutro/negativo)
    - summary (até 3 linhas)
    - similar_post_text (até 280 chars)
    - similar_image_prompt (descrição da imagem sugerida)
    - usage de tokens (quando disponível)
    """
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
        if out_parts and isinstance(out_parts[0], dict):
            raw_text = out_parts[0].get("text", "") or ""

    # tenta parsear como JSON, se falhar retorna bruto
    try:
        parsed = json.loads(raw_text) if raw_text else {}
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


@app.post("/v1/ai/gemini/analyze")
def api_gemini_analyze(req: GeminiAnalyzeRequest):
    """
    POC: Analisa texto + imagem e retorna:
    - sentimento
    - resumo
    - sugestão de post similar (texto)
    - prompt/descrição de imagem sugerida
    - uso de tokens (quando disponível)
    """
    try:
        model = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")

        image_b64 = None
        image_mime = req.image_mime or "image/jpeg"

        if req.image_base64:
            image_b64 = req.image_base64
        elif req.image_url:
            image_b64 = _download_image_as_base64(req.image_url)

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

    except Exception as e:
        detail = {
            "message": "Falha ao executar análise no Gemini",
            "error_type": type(e).__name__,
            "error": str(e),
        }
        if req.debug:
            detail["stack"] = traceback.format_exc()
        raise HTTPException(status_code=502, detail=detail)
