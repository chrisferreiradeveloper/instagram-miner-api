import json
import time
from datetime import datetime, timezone
import instaloader


def mine_profile(username: str, max_posts: int = 30, sleep_s: float = 2.0) -> dict:
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    # Se começar a dar bloqueio/429, descomente e use login:
    # L.login("SEU_USUARIO", "SUA_SENHA")

    profile = instaloader.Profile.from_username(L.context, username)

    out = {
        "profile": {
            "username": profile.username,
            "full_name": profile.full_name,
            "biography": profile.biography,
            "external_url": profile.external_url,
            "followers": profile.followers,
            "followees": profile.followees,
            "mediacount": profile.mediacount,
            "is_verified": profile.is_verified,
            "is_private": profile.is_private,
            "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "posts": [],
    }

    for i, post in enumerate(profile.get_posts(), start=1):
        if i > max_posts:
            break

        out["posts"].append(
            {
                "shortcode": post.shortcode,
                "url": f"https://www.instagram.com/p/{post.shortcode}/",
                "date_utc": post.date_utc.replace(tzinfo=timezone.utc).isoformat(),
                "likes": post.likes,
                "comments": post.comments,
                "caption": post.caption or "",
                "typename": post.typename,  # GraphImage / GraphVideo / GraphSidecar
                "is_video": post.is_video,
            }
        )

        time.sleep(sleep_s)

    return out


if __name__ == "__main__":
    raw = input("Digite o @ do perfil (ex: @instagram ou instagram): ").strip()
    username = raw.lstrip("@").strip()

    raw_posts = input("Quantos posts puxar? (padrão 30): ").strip()
    max_posts = int(raw_posts) if raw_posts else 30

    raw_sleep = input("Delay entre requisições em segundos? (padrão 2.0): ").strip()
    sleep_s = float(raw_sleep) if raw_sleep else 2.0

    data = mine_profile(username=username, max_posts=max_posts, sleep_s=sleep_s)

    filename = f"{username}_dump.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"OK ✅ JSON gerado: {filename} (posts: {len(data['posts'])})")
