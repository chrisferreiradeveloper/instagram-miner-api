import json
import time
from datetime import datetime, timezone
from typing import Literal, Optional

import instaloader
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Instagram Miner API", version="1.0.0")

SortMode = Literal["recent", "most_liked", "most_commented", "best_engagement"]

def build_loader(session_user: Optional[str] = None) -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    # Carrega sessão salva (recomendado para reduzir 403/429)
    # Você cria o arquivo de sessão com o script de login (te mando abaixo).
    if session_user:
        try:
            L.load_session_from_file(session_user)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Falha ao carregar sessão '{session_user}': {e}")

    return L

def mine_profile(
    username: str,
    max_posts: int = 30,
    sleep_s: float = 2.0,
    sort: SortMode = "recent",
    include_caption: bool = True,
    caption_max_len: int = 2000,
    session_user: Optional[str] = None,
) -> dict:
    username = username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username é obrigatório")

    L = build_loader(session_user=session_user)

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao carregar perfil '{username}': {e}")

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
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao varrer posts de '{username}': {e}")

    # Calcula engajamento simples usando followers atuais
    followers = profile.followers or 0
    for p in posts:
        if followers > 0:
            p["engagement_rate"] = (p["likes"] + p["comments"]) / followers
        else:
            p["engagement_rate"] = None

    # Ordenação no servidor (depois da coleta)
    if sort == "recent":
        posts.sort(key=lambda x: x["date_utc"], reverse=True)
    elif sort == "most_liked":
        posts.sort(key=lambda x: x["likes"], reverse=True)
    elif sort == "most_commented":
        posts.sort(key=lambda x: x["comments"], reverse=True)
    elif sort == "best_engagement":
        posts.sort(key=lambda x: (x["engagement_rate"] is not None, x["engagement_rate"]), reverse=True)

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
):
    return mine_profile(
        username=username,
        max_posts=max_posts,
        sleep_s=sleep_s,
        sort=sort,
        include_caption=include_caption,
        caption_max_len=caption_max_len,
        session_user=session_user,
    )
