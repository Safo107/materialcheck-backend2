from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime
import json
import re
import uvicorn

# -----------------------------
# ENV LOAD
# -----------------------------

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# -----------------------------
# DATABASE
# -----------------------------

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

# -----------------------------
# OPENAI
# -----------------------------

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# FASTAPI
# -----------------------------

app = FastAPI()
api_router = APIRouter(prefix="/api")

# -----------------------------
# MODELS
# -----------------------------

class Article(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    folder_id: str = ""
    name: str
    current_stock: int = 0
    min_stock: int = 0
    unit: str = "Stück"
    category: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ArticleCreate(BaseModel):
    name: str
    folder_id: str = ""
    current_stock: int = 0
    min_stock: int = 0
    unit: str = "Stück"
    category: str = ""


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    actions_taken: List[str] = []


class ProfileModel(BaseModel):
    deviceId: str
    firmName: str = ""
    userName: str = ""
    email: str = ""
    logoUri: Optional[str] = None
    updatedAt: str = ""


# -----------------------------
# ROOT
# -----------------------------

@api_router.get("/")
async def root():
    return {"message": "MaterialCheck API läuft"}

# -----------------------------
# HEALTH CHECK (für Render)
# -----------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}

# -----------------------------
# ARTICLES
# -----------------------------

@api_router.get("/articles", response_model=List[Article])
async def get_articles():
    articles = await db.articles.find().limit(50).to_list(50)
    return [Article(**a) for a in articles]


@api_router.post("/articles", response_model=Article)
async def create_article(input: ArticleCreate):
    article = Article(**input.model_dump())
    await db.articles.insert_one(article.model_dump())
    return article


@api_router.delete("/articles/{article_id}")
async def delete_article(article_id: str):
    result = await db.articles.delete_one({"id": article_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Artikel nicht gefunden")
    return {"message": "Artikel gelöscht"}

# -----------------------------
# AI CHAT
# -----------------------------

@api_router.post("/chat", response_model=ChatResponse)
async def chat_with_ai(request: ChatRequest):

    articles = await db.articles.find().limit(50).to_list(50)
    articles_list = [Article(**a) for a in articles]

    inventory_context = "Aktuelle Artikel im Inventar:\n"
    for a in articles_list:
        inventory_context += f"- {a.name}: {a.current_stock} {a.unit}\n"

    system_message = f"""
Du bist ein KI-Assistent für eine Materiallager App für Elektriker.

Du kannst:
- Artikel hinzufügen
- Bestand erhöhen oder reduzieren
- Einkaufslisten erstellen

{inventory_context}

Wenn du eine Aktion ausführen willst, antworte im JSON Format:

{{
"action": "add_article | adjust_stock | none",
"data": {{}},
"message": "Antwort an den Benutzer"
}}
"""

    try:
        completion = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=200,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": request.message},
            ],
        )

        response = completion.choices[0].message.content
        actions_taken = []
        final_response = response

        json_match = re.search(r"\{.*\}", response, re.DOTALL)

        if json_match:
            action_data = json.loads(json_match.group(0))
            action = action_data.get("action")
            data = action_data.get("data", {})
            message = action_data.get("message", response)

            if action == "add_article":
                article = ArticleCreate(
                    name=data.get("name", "Neuer Artikel"),
                    current_stock=data.get("current_stock", 0),
                    min_stock=data.get("min_stock", 5),
                )
                new_article = Article(**article.model_dump())
                await db.articles.insert_one(new_article.model_dump())
                actions_taken.append(f"Artikel {article.name} hinzugefügt")

            if action == "adjust_stock":
                article_name = data.get("article_name")
                amount = data.get("amount", 0)
                article = await db.articles.find_one(
                    {"name": {"$regex": f"^{re.escape(article_name)}$", "$options": "i"}}
                )
                if article:
                    new_stock = max(0, article["current_stock"] + amount)
                    await db.articles.update_one(
                        {"id": article["id"]},
                        {"$set": {"current_stock": new_stock}},
                    )
                    actions_taken.append(f"Bestand von {article['name']} geändert ({amount})")

            final_response = message

        return ChatResponse(response=final_response, actions_taken=actions_taken)

    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="KI Fehler")

# -----------------------------
# PROFIL
# -----------------------------

@api_router.post("/profile")
async def save_profile(profile: ProfileModel):
    """Profil speichern oder aktualisieren"""
    try:
        await db.profiles.update_one(
            {"deviceId": profile.deviceId},
            {"$set": profile.model_dump()},
            upsert=True
        )
        return {"success": True, "message": "Profil gespeichert"}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Profil konnte nicht gespeichert werden")


@api_router.get("/profile/{device_id}")
async def get_profile(device_id: str):
    """Profil laden"""
    try:
        profile = await db.profiles.find_one({"deviceId": device_id})
        if not profile:
            raise HTTPException(status_code=404, detail="Profil nicht gefunden")
        profile.pop("_id", None)
        return profile
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Profil konnte nicht geladen werden")

# -----------------------------
# ROUTER
# -----------------------------

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# SHUTDOWN
# -----------------------------

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

# -----------------------------
# START SERVER (Render)
# -----------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
