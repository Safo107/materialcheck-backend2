from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Request, Query
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

import os
import logging
from urllib.parse import unquote
import hashlib
import json
import re
import uuid
import random
import string
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
from datetime import datetime, timedelta
import uvicorn
import httpx

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
# HELPERS
# -----------------------------

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def norm_email(email: str) -> str:
    return email.strip().lower()

def gen_code(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

async def send_reset_email(to_email: str, code: str) -> bool:
    if not RESEND_API_KEY:
        logging.warning(f"RESEND_API_KEY not set — PIN reset code for {to_email}: {code}")
        return True  # In dev mode, pretend it was sent
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": "MaterialCheck <safin.d@elektrogenius.de>",
                    "to": [to_email],
                    "subject": "MaterialCheck — PIN zurücksetzen",
                    "html": f"""
                    <div style="font-family:sans-serif;max-width:400px;margin:auto;padding:24px">
                    <h2 style="color:#f5a623">MaterialCheck PIN Reset</h2>
                    <p>Dein Reset-Code:</p>
                    <div style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#0d1117;background:#f5a623;padding:16px;border-radius:8px;text-align:center">{code}</div>
                    <p style="color:#666;font-size:12px">Der Code ist 15 Minuten gültig. Falls du keinen Reset angefordert hast, ignoriere diese Email.</p>
                    </div>
                    """,
                },
                timeout=10,
            )
            return res.status_code < 300
    except Exception as e:
        logging.error(f"Email send error: {e}")
        return False

# WebSocket connections: { companyId: [ws, ws, ...] }
company_connections: Dict[str, List[WebSocket]] = {}

# WebSocket connections pro Lager: { warehouseId: [{ws, email, name}, ...] }
warehouse_connections: Dict[str, List[dict]] = {}

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
    hasPin: bool = False
    companyId: Optional[str] = None
    companyRole: Optional[str] = None
    pin: Optional[str] = None  # hashed PIN, only set when explicitly provided

class ProfileLoadRequest(BaseModel):
    email: str
    pin: str
    deviceId: str

class MaterialsSyncRequest(BaseModel):
    deviceId: str
    email: str
    folders: List[Any] = []
    materials: List[Any] = []
    tasks: List[Any] = []
    suppliers: List[Any] = []
    loans: List[Any] = []
    syncedAt: str = ""

class MaterialsLoadRequest(BaseModel):
    email: str
    pin: str

class CompanyCreateRequest(BaseModel):
    ownerEmail: str
    ownerName: str
    companyName: str
    deviceId: str

class InviteRequest(BaseModel):
    companyId: str
    inviterEmail: str
    inviteeEmail: str
    role: str = "member"

class AcceptInviteRequest(BaseModel):
    inviteId: str
    email: str
    deviceId: str
    userName: str = ""

class RejectInviteRequest(BaseModel):
    inviteId: str
    email: Optional[str] = None

class ChangeRoleRequest(BaseModel):
    companyId: str
    ownerEmail: str
    targetEmail: str
    newRole: str

class WarehouseSyncRequest(BaseModel):
    companyId: str
    warehouseId: str
    email: str
    materials: List[Any] = []
    tasks: List[Any] = []
    activities: List[Any] = []
    syncedAt: str = ""

class PinResetRequestModel(BaseModel):
    email: str

class PinResetConfirmModel(BaseModel):
    email: str
    code: str
    newPin: str
    deviceId: str

# -----------------------------
# ROOT
# -----------------------------

@api_router.get("/")
async def root():
    return {"message": "MaterialCheck API läuft"}

# -----------------------------
# HEALTH CHECK
# -----------------------------

@app.get("/health")
@api_router.get("/health")
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
            model="gpt-4o-mini", max_tokens=200, temperature=0.2,
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
            try:
                action_data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                action_data = {}
            action = action_data.get("action")
            data = action_data.get("data", {})
            message = action_data.get("message", response)
            if action == "add_article":
                article = ArticleCreate(name=data.get("name", "Neuer Artikel"), current_stock=data.get("current_stock", 0), min_stock=data.get("min_stock", 5))
                new_article = Article(**article.model_dump())
                await db.articles.insert_one(new_article.model_dump())
                actions_taken.append(f"Artikel {article.name} hinzugefügt")
            if action == "adjust_stock":
                article_name = data.get("article_name")
                amount = data.get("amount", 0)
                article = await db.articles.find_one({"name": {"$regex": f"^{re.escape(article_name)}$", "$options": "i"}})
                if article:
                    new_stock = max(0, article["current_stock"] + amount)
                    await db.articles.update_one({"id": article["id"]}, {"$set": {"current_stock": new_stock}})
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
    """Profil speichern — PIN wird gehashed wenn mitgeliefert"""
    try:
        data = profile.model_dump()
        if data.get("email"):
            data["email"] = norm_email(data["email"])
        # Hash PIN only when it arrives as plaintext (hasPin not yet set by client)
        if data.get("pin") and not data.get("hasPin"):
            data["pin"] = hash_pin(data["pin"])
        # Don't overwrite existing PIN with None
        existing = None
        if data.get("email"):
            existing = await db.profiles.find_one({"email": data["email"]})
        if not existing:
            existing = await db.profiles.find_one({"deviceId": data["deviceId"]})
        if existing and not data.get("pin") and existing.get("pin"):
            data["pin"] = existing["pin"]
            data["hasPin"] = True
        await db.profiles.update_one(
            {"deviceId": data["deviceId"]},
            {"$set": data},
            upsert=True
        )
        if data.get("email"):
            # Nur sync — kein upsert, um Duplikate bei neuer Email zu vermeiden
            await db.profiles.update_one({"email": data["email"]}, {"$set": data})
        return {"success": True}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Profil konnte nicht gespeichert werden")

@api_router.get("/profile/check/{email}")
async def check_profile_by_email(email: str):
    """Prüfen ob Profil mit dieser Email existiert"""
    try:
        profile = await db.profiles.find_one({"email": norm_email(email)})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        profile.pop("_id", None)
        return {
            "exists": True,
            "hasPin": bool(profile.get("pin") or profile.get("hasPin")),
            "firmName": profile.get("firmName", ""),
            "userName": profile.get("userName", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/profile/load")
async def load_profile_with_pin(req: ProfileLoadRequest):
    """Profil mit Email + PIN laden"""
    try:
        profile = await db.profiles.find_one({"email": norm_email(req.email)})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        stored_pin = profile.get("pin")
        if not stored_pin:
            raise HTTPException(status_code=401, detail="Kein PIN gesetzt")
        if stored_pin != hash_pin(req.pin):
            raise HTTPException(status_code=401, detail="Falscher PIN")
        profile.pop("_id", None)
        profile.pop("pin", None)
        profile["hasPin"] = True
        profile["deviceId"] = req.deviceId  # Update device ID
        # Update device ID in DB
        await db.profiles.update_one({"email": norm_email(req.email)}, {"$set": {"deviceId": req.deviceId}})
        return profile
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler beim Laden")

@api_router.get("/profile/{device_id}")
async def get_profile(device_id: str):
    """Profil per DeviceID laden"""
    try:
        profile = await db.profiles.find_one({"deviceId": device_id})
        if not profile:
            raise HTTPException(status_code=404, detail="Profil nicht gefunden")
        profile.pop("_id", None)
        profile.pop("pin", None)
        return profile
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Profil konnte nicht geladen werden")

# -----------------------------
# PIN RESET
# -----------------------------

@api_router.post("/profile/reset-pin/request")
async def reset_pin_request(req: PinResetRequestModel):
    """Reset-Code per Email senden"""
    try:
        email = norm_email(req.email)
        profile = await db.profiles.find_one({"email": email})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        code = gen_code(6)
        expires = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        await db.pin_resets.update_one(
            {"email": email},
            {"$set": {"email": email, "code": code, "expires": expires, "used": False}},
            upsert=True
        )
        ok = await send_reset_email(email, code)
        if not ok:
            raise HTTPException(status_code=500, detail="Email konnte nicht gesendet werden")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/profile/reset-pin/confirm")
async def reset_pin_confirm(req: PinResetConfirmModel):
    """PIN mit Code zurücksetzen"""
    try:
        email = norm_email(req.email)
        reset = await db.pin_resets.find_one({"email": email, "used": False})
        if not reset:
            raise HTTPException(status_code=400, detail="Kein aktiver Reset-Code")
        if reset.get("code") != req.code:
            raise HTTPException(status_code=401, detail="Falscher Code")
        if datetime.fromisoformat(reset["expires"]) < datetime.utcnow():
            raise HTTPException(status_code=400, detail="Code abgelaufen")
        new_pin_hash = hash_pin(req.newPin)
        await db.profiles.update_one(
            {"email": email},
            {"$set": {"pin": new_pin_hash, "hasPin": True, "deviceId": req.deviceId}}
        )
        await db.pin_resets.update_one({"email": email}, {"$set": {"used": True}})
        profile = await db.profiles.find_one({"email": email})
        profile.pop("_id", None)
        profile.pop("pin", None)
        return {"success": True, "profile": profile}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

# -----------------------------
# MATERIALIEN SYNC
# -----------------------------

@api_router.post("/materials/sync")
async def sync_materials(req: MaterialsSyncRequest):
    """Materialien in Cloud speichern + Echtzeit-Broadcast an andere Geräte"""
    try:
        synced_at = req.syncedAt or datetime.utcnow().isoformat()
        email_norm = norm_email(req.email) if req.email else req.email
        data = {
            "deviceId": req.deviceId,
            "email": email_norm,
            "folders": req.folders,
            "materials": req.materials,
            "tasks": req.tasks,
            "suppliers": req.suppliers,
            "loans": req.loans,
            "syncedAt": synced_at,
        }
        filter_q = {"email": email_norm} if email_norm else {"deviceId": req.deviceId}
        await db.materials_sync.update_one(filter_q, {"$set": data}, upsert=True)

        # Echtzeit-Broadcast an alle anderen Geräte mit gleicher E-Mail
        if email_norm:
            await broadcast_company_update(email_norm, {
                "type": "data_updated",
                "deviceId": req.deviceId,
                "syncedAt": synced_at,
            })

        return {"success": True, "message": f"{len(req.materials)} Materialien gespeichert"}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Sync fehlgeschlagen")


class MaterialsPullRequest(BaseModel):
    email: str
    deviceId: str

@api_router.post("/materials/pull")
async def pull_materials(req: MaterialsPullRequest):
    """Neueste Daten abrufen — kein PIN nötig (für Echtzeit-Sync)"""
    try:
        email_norm = norm_email(req.email)
        sync_data = await db.materials_sync.find_one({"email": email_norm})
        if not sync_data:
            return {"found": False}
        sync_data.pop("_id", None)
        return {"found": True, **sync_data}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Pull fehlgeschlagen")

@api_router.post("/materials/load")
async def load_materials(req: MaterialsLoadRequest):
    """Materialien mit Email + PIN laden"""
    try:
        profile = await db.profiles.find_one({"email": req.email})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        if profile.get("pin") and profile["pin"] != hash_pin(req.pin):
            raise HTTPException(status_code=401, detail="Falscher PIN")
        sync_data = await db.materials_sync.find_one({"email": req.email})
        if not sync_data:
            return {"folders": [], "materials": [], "tasks": [], "suppliers": [], "loans": [], "syncedAt": ""}
        sync_data.pop("_id", None)
        return sync_data
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Laden fehlgeschlagen")

# -----------------------------
# FIRMA / COMPANY
# -----------------------------

@api_router.post("/company/create")
async def create_company(req: CompanyCreateRequest):
    """Firma erstellen — nur für Pro/Trial-Nutzer"""
    try:
        # ── PFLICHT: Pro-Status prüfen ────────────────────────────────────────
        profile = await db.profiles.find_one({"email": norm_email(req.ownerEmail)})
        is_premium = profile.get("isPremium", False) if profile else False
        in_trial   = profile.get("inTrial",   False) if profile else False
        # Trial automatisch ablaufen lassen
        if in_trial:
            trial_ends_at = profile.get("trialEndsAt") if profile else None
            if trial_ends_at:
                try:
                    ends = trial_ends_at if isinstance(trial_ends_at, datetime) \
                           else datetime.fromisoformat(str(trial_ends_at).replace("Z", ""))
                    if datetime.utcnow() > ends:
                        in_trial   = False
                        is_premium = False
                except Exception:
                    pass
        if not is_premium and not in_trial:
            raise HTTPException(
                status_code=403,
                detail="MaterialCheck+ erforderlich. Bitte upgraden um eine Firma zu erstellen."
            )
        # ─────────────────────────────────────────────────────────────────────

        # Check if owner already has a company
        existing = await db.companies.find_one({"ownerEmail": req.ownerEmail})
        if existing:
            existing.pop("_id", None)
            return {"success": True, "company": existing}
        company_id = str(uuid.uuid4())
        company = {
            "companyId": company_id,
            "companyName": req.companyName,
            "ownerEmail": req.ownerEmail,
            "ownerName": req.ownerName,
            "members": [{
                "email": req.ownerEmail,
                "name": req.ownerName,
                "role": "owner",
                "deviceId": req.deviceId,
                "joinedAt": datetime.utcnow().isoformat(),
            }],
            "warehouses": [],
            "createdAt": datetime.utcnow().isoformat(),
        }
        await db.companies.insert_one(company)
        company.pop("_id", None)
        return {"success": True, "company": company}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Firma konnte nicht erstellt werden")

@api_router.get("/company/by-owner/{email}")
async def get_company_by_owner(email: str):
    """Firma per Owner-Email laden"""
    try:
        company = await db.companies.find_one({"ownerEmail": norm_email(email)})
        if not company:
            # Check if member
            company = await db.companies.find_one({"members.email": norm_email(email)})
        if not company:
            raise HTTPException(status_code=404, detail="Keine Firma gefunden")
        company.pop("_id", None)
        return company
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.get("/company/invites/{email}")
async def get_invites(email: str):
    """Ausstehende Einladungen für Email laden"""
    try:
        email = norm_email(unquote(email))
        invites = await db.invites.find({"inviteeEmail": email, "status": "pending"}).to_list(50)
        for inv in invites:
            inv.pop("_id", None)
        return invites
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.get("/company/{company_id}")
async def get_company(company_id: str, email: str = ""):
    """Firma per ID laden"""
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        # Access check
        if email:
            member_emails = [m.get("email") for m in company.get("members", [])]
            if email not in member_emails:
                raise HTTPException(status_code=403, detail="Kein Zugriff")
        company.pop("_id", None)
        return company
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/company/invite")
async def invite_member(req: InviteRequest):
    """Mitglied einladen"""
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        # Check permissions
        members = company.get("members", [])
        inviter = next((m for m in members if m["email"] == req.inviterEmail), None)
        if not inviter or inviter["role"] not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Keine Berechtigung")
        # Check if already member
        if any(m["email"] == req.inviteeEmail for m in members):
            raise HTTPException(status_code=400, detail="Bereits Mitglied")
        # Check for existing pending invite
        existing_invite = await db.invites.find_one({
            "companyId": req.companyId,
            "inviteeEmail": req.inviteeEmail,
            "status": "pending"
        })
        if existing_invite:
            existing_invite.pop("_id", None)
            return {"success": True, "invite": existing_invite}
        invite_id = str(uuid.uuid4())
        invite = {
            "inviteId": invite_id,
            "companyId": req.companyId,
            "companyName": company.get("companyName", ""),
            "inviterEmail": req.inviterEmail,
            "inviteeEmail": req.inviteeEmail,
            "role": req.role,
            "status": "pending",
            "createdAt": datetime.utcnow().isoformat(),
        }
        await db.invites.insert_one(invite)
        invite.pop("_id", None)
        return {"success": True, "invite": invite}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Einladung fehlgeschlagen")

@api_router.post("/company/accept")
async def accept_invite(req: AcceptInviteRequest):
    """Einladung annehmen"""
    try:
        invite = await db.invites.find_one({"inviteId": req.inviteId, "inviteeEmail": req.email})
        if not invite:
            raise HTTPException(status_code=404, detail="Einladung nicht gefunden")
        if invite.get("status") != "pending":
            raise HTTPException(status_code=400, detail="Einladung bereits bearbeitet")
        # Add member to company
        new_member = {
            "email": req.email,
            "name": req.userName or req.email.split("@")[0],
            "role": invite.get("role", "member"),
            "deviceId": req.deviceId,
            "joinedAt": datetime.utcnow().isoformat(),
        }
        await db.companies.update_one(
            {"companyId": invite["companyId"]},
            {"$push": {"members": new_member}}
        )
        await db.invites.update_one({"inviteId": req.inviteId}, {"$set": {"status": "accepted"}})
        company = await db.companies.find_one({"companyId": invite["companyId"]})
        company.pop("_id", None)
        # Notify company members via WebSocket
        await broadcast_company_update(invite["companyId"], {"type": "member_joined", "email": req.email})
        return {"success": True, "company": company}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler beim Annehmen")

@api_router.post("/company/reject")
async def reject_invite(req: RejectInviteRequest):
    """Einladung ablehnen"""
    try:
        await db.invites.update_one(
            {"inviteId": req.inviteId, "inviteeEmail": req.email},
            {"$set": {"status": "rejected"}}
        )
        return {"success": True}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/company/role")
async def change_role(req: ChangeRoleRequest):
    """Rolle eines Mitglieds ändern"""
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        if company.get("ownerEmail") != req.ownerEmail:
            owner_member = next((m for m in company.get("members", []) if m["email"] == req.ownerEmail), None)
            if not owner_member or owner_member["role"] not in ("owner", "admin"):
                raise HTTPException(status_code=403, detail="Keine Berechtigung")
        if req.newRole == "owner":
            raise HTTPException(status_code=400, detail="Owner kann nicht geändert werden")
        await db.companies.update_one(
            {"companyId": req.companyId, "members.email": req.targetEmail},
            {"$set": {"members.$.role": req.newRole}}
        )
        await broadcast_company_update(req.companyId, {"type": "role_changed", "email": req.targetEmail, "role": req.newRole})
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/company/{company_id}/leave")
async def leave_company(company_id: str, email: str):
    """Firma verlassen — Owner verlassen = Firma wird aufgelöst (alle Mitglieder raus)"""
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        if norm_email(company.get("ownerEmail", "")) == norm_email(email):
            # Owner verlässt = Firma komplett auflösen
            await db.companies.delete_one({"companyId": company_id})
            await broadcast_company_update(company_id, {"type": "company_dissolved", "reason": "owner_left"})
            return {"success": True, "action": "dissolved"}
        await db.companies.update_one(
            {"companyId": company_id},
            {"$pull": {"members": {"email": norm_email(email)}}}
        )
        await broadcast_company_update(company_id, {"type": "member_left", "email": email})
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.delete("/company/{company_id}/member/{member_email}")
async def remove_member(company_id: str, member_email: str, owner_email: str = ""):
    """Mitglied entfernen"""
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        if owner_email:
            owner = next((m for m in company.get("members", []) if m["email"] == owner_email), None)
            if not owner or owner["role"] not in ("owner", "admin"):
                raise HTTPException(status_code=403, detail="Keine Berechtigung")
        await db.companies.update_one(
            {"companyId": company_id},
            {"$pull": {"members": {"email": member_email}}}
        )
        await broadcast_company_update(company_id, {"type": "member_removed", "email": member_email})
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.delete("/company/{company_id}")
async def dissolve_company(company_id: str, owner_email: str = ""):
    """Firma auflösen: Immer komplett löschen — alle Mitglieder fliegen raus"""
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")

        # Nur Owner darf auflösen
        if norm_email(company.get("ownerEmail","")) != norm_email(owner_email):
            raise HTTPException(status_code=403, detail="Nur der Chef kann die Firma auflösen")

        await db.companies.delete_one({"companyId": company_id})
        await broadcast_company_update(company_id, {"type": "company_dissolved", "reason": "owner_dissolved"})
        return {"success": True, "action": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler beim Auflösen")

# -----------------------------
# LAGER (WAREHOUSE)
# -----------------------------

@api_router.post("/company/{company_id}/warehouse/create")
async def create_warehouse(company_id: str, email: str, name: str, icon: str = "🏭"):
    """Lager erstellen"""
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        if not member or member["role"] not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Keine Berechtigung")
        warehouse_id = str(uuid.uuid4())
        warehouse = {
            "warehouseId": warehouse_id,
            "name": name,
            "icon": icon,
            "createdAt": datetime.utcnow().isoformat(),
            "createdBy": email,
        }
        await db.companies.update_one(
            {"companyId": company_id},
            {"$push": {"warehouses": warehouse}}
        )
        await broadcast_company_update(company_id, {"type": "warehouse_created", "warehouse": warehouse})
        return {"success": True, "warehouse": warehouse}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.get("/company/{company_id}/warehouse/{warehouse_id}")
async def get_warehouse(company_id: str, warehouse_id: str, email: str = ""):
    """Lager-Materialien laden"""
    try:
        if email:
            company = await db.companies.find_one({"companyId": company_id})
            if company:
                member_emails = [norm_email(m.get("email","")) for m in company.get("members", [])]
                if norm_email(email) not in member_emails:
                    raise HTTPException(status_code=403, detail="Kein Zugriff")
        sync_data = await db.warehouse_materials.find_one({
            "companyId": company_id, "warehouseId": warehouse_id
        })
        if not sync_data:
            return {"materials": [], "tasks": [], "activities": [], "syncedAt": ""}
        sync_data.pop("_id", None)
        return sync_data
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/company/warehouse/sync")
async def sync_warehouse(req: WarehouseSyncRequest):
    """Lager-Materialien synchronisieren"""
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        member = next((m for m in company.get("members", []) if norm_email(m.get("email","")) == norm_email(req.email)), None)
        if not member:
            raise HTTPException(status_code=403, detail="Kein Zugriff")
        data = {
            "companyId": req.companyId,
            "warehouseId": req.warehouseId,
            "materials": req.materials,
            "tasks": req.tasks,
            "activities": req.activities,
            "syncedAt": req.syncedAt or datetime.utcnow().isoformat(),
            "syncedBy": req.email,
        }
        await db.warehouse_materials.update_one(
            {"companyId": req.companyId, "warehouseId": req.warehouseId},
            {"$set": data}, upsert=True
        )
        await broadcast_warehouse_update(req.warehouseId, {
            "type": "warehouse_updated",
            "materials": req.materials,
            "tasks": req.tasks,
            "activities": req.activities,
            "updatedBy": req.email,
        })
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Sync fehlgeschlagen")

# -----------------------------
# WEBSOCKET (Echtzeit-Sync)
# -----------------------------

async def broadcast_company_update(company_id: str, message: dict):
    """Alle Verbindungen in einer Firma benachrichtigen"""
    connections = company_connections.get(company_id, [])
    disconnected = []
    for ws in connections:
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connections.remove(ws)

async def broadcast_warehouse_update(warehouse_id: str, message: dict, exclude: WebSocket = None):
    """Alle Verbindungen in einem Lager benachrichtigen"""
    users = warehouse_connections.get(warehouse_id, [])
    disconnected = []
    for u in users:
        if u["ws"] == exclude:
            continue
        try:
            await u["ws"].send_text(json.dumps(message))
        except Exception:
            disconnected.append(u)
    for u in disconnected:
        if u in users:
            users.remove(u)

async def broadcast_active_users(warehouse_id: str):
    users = warehouse_connections.get(warehouse_id, [])
    active = [{"email": u["email"], "name": u["name"]} for u in users]
    msg = json.dumps({"type": "active_users", "users": active})
    disconnected = []
    for u in users:
        try:
            await u["ws"].send_text(msg)
        except Exception:
            disconnected.append(u)
    for u in disconnected:
        if u in users:
            users.remove(u)

@app.websocket("/ws/warehouse/{warehouse_id}")
async def websocket_warehouse(websocket: WebSocket, warehouse_id: str, email: str = "", name: str = ""):
    await websocket.accept()
    if warehouse_id not in warehouse_connections:
        warehouse_connections[warehouse_id] = []
    user_info = {"ws": websocket, "email": email, "name": name or email}
    warehouse_connections[warehouse_id].append(user_info)
    await broadcast_active_users(warehouse_id)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    continue
                msg["changedBy"] = email
                msg["changedByName"] = name or email
                await broadcast_warehouse_update(warehouse_id, msg, exclude=websocket)
            except (json.JSONDecodeError, ValueError):
                pass
    except WebSocketDisconnect:
        if warehouse_id in warehouse_connections:
            warehouse_connections[warehouse_id] = [
                u for u in warehouse_connections[warehouse_id] if u["ws"] != websocket
            ]
        await broadcast_active_users(warehouse_id)

@app.websocket("/ws/company/{company_id}")
async def websocket_company(websocket: WebSocket, company_id: str):
    await websocket.accept()
    if company_id not in company_connections:
        company_connections[company_id] = []
    company_connections[company_id].append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                # Broadcast to all other members
                await broadcast_company_update(company_id, msg)
            except (json.JSONDecodeError, ValueError):
                pass
    except WebSocketDisconnect:
        if company_id in company_connections:
            try:
                company_connections[company_id].remove(websocket)
            except ValueError:
                pass

# -----------------------------
# STRIPE
# -----------------------------
# Render.com: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_MATERIALCHECK_PLUS

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MATERIALCHECK_PLUS = os.environ.get("STRIPE_PRICE_MATERIALCHECK_PLUS", "")

# Startup-Check: fehlende Stripe-Variablen loggen
_stripe_missing = [v for v, val in {
    "STRIPE_SECRET_KEY": STRIPE_SECRET_KEY,
    "STRIPE_WEBHOOK_SECRET": STRIPE_WEBHOOK_SECRET,
    "STRIPE_PRICE_MATERIALCHECK_PLUS": STRIPE_PRICE_MATERIALCHECK_PLUS,
}.items() if not val]
if _stripe_missing:
    logging.warning(f"[Stripe] Fehlende Umgebungsvariablen: {', '.join(_stripe_missing)} — Checkout deaktiviert")

try:
    import stripe as stripe_lib
    stripe_lib.api_key = STRIPE_SECRET_KEY if STRIPE_SECRET_KEY else None
    _stripe_available = bool(STRIPE_SECRET_KEY)
except ImportError:
    logging.error("[Stripe] stripe-Paket nicht installiert — pip install stripe")
    stripe_lib = None  # type: ignore
    _stripe_available = False

# -----------------------------
# RESEND
# -----------------------------
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

try:
    import resend as resend_lib
    resend_lib.api_key = RESEND_API_KEY
    _resend_available = bool(RESEND_API_KEY)
    if not _resend_available:
        logging.warning("[Resend] RESEND_API_KEY nicht gesetzt — E-Mail-Versand deaktiviert")
except ImportError:
    logging.error("[Resend] resend-Paket nicht installiert — pip install resend")
    resend_lib = None  # type: ignore
    _resend_available = False


@api_router.get("/pro/status")
async def get_pro_status(email: str = Query(..., description="E-Mail des Users")):
    """Pro-Status eines Users aus der DB lesen — wird nach Checkout vom Frontend aufgerufen"""
    try:
        profile = await db.profiles.find_one({"email": norm_email(email)})
        if not profile:
            return {"isPro": False, "inTrial": False, "trialEndsAt": None}
        is_premium = profile.get("isPremium", False)
        in_trial = profile.get("inTrial", False)
        trial_ends_at = profile.get("trialEndsAt")
        if isinstance(trial_ends_at, datetime):
            trial_ends_at = trial_ends_at.isoformat()
        # Auto-expire trial
        if in_trial and trial_ends_at:
            try:
                if datetime.utcnow() > datetime.fromisoformat(trial_ends_at.replace("Z", "")):
                    in_trial = False
                    is_premium = False
            except Exception:
                pass
        return {"isPro": is_premium, "inTrial": in_trial, "trialEndsAt": trial_ends_at}
    except Exception as e:
        logging.error(f"[pro/status] {e}")
        return {"isPro": False, "inTrial": False, "trialEndsAt": None}


class CheckoutSessionRequest(BaseModel):
    email: str
    deviceId: str
    product: str = "materialcheck-plus"


@api_router.post("/stripe/create-checkout-session")
async def create_checkout_session(body: CheckoutSessionRequest):
    if not _stripe_available or not stripe_lib:
        raise HTTPException(status_code=503, detail="Stripe nicht konfiguriert. Bitte STRIPE_SECRET_KEY auf Render.com setzen.")
    if not STRIPE_PRICE_MATERIALCHECK_PLUS:
        print("[MaterialCheck+ Stripe] FEHLER: STRIPE_PRICE_MATERIALCHECK_PLUS ist nicht gesetzt. Bitte in den Umgebungsvariablen hinterlegen.")
        raise HTTPException(
            status_code=400,
            detail="Stripe-Preis-ID nicht konfiguriert. Bitte prüfe die Umgebungsvariable STRIPE_PRICE_MATERIALCHECK_PLUS.",
        )

    import os as _os
    if _os.environ.get("ENVIRONMENT", "production") != "production":
        print(f"[DEV] Stripe-Initiierung für MaterialCheck+ mit ID: {STRIPE_PRICE_MATERIALCHECK_PLUS} erfolgt.")

    profile = await db.profiles.find_one({"email": norm_email(body.email)})
    customer_id = profile.get("stripeCustomerId") if profile else None

    if not customer_id:
        customer = stripe_lib.Customer.create(
            email=body.email,
            metadata={"email": body.email, "deviceId": body.deviceId},
        )
        customer_id = customer.id
        await db.profiles.update_one(
            {"email": norm_email(body.email)},
            {"$set": {"stripeCustomerId": customer_id}},
        )

    session = stripe_lib.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": STRIPE_PRICE_MATERIALCHECK_PLUS, "quantity": 1}],
        mode="subscription",
        metadata={"email": body.email, "deviceId": body.deviceId, "product": body.product},
        subscription_data={
            "trial_period_days": 7,
            "metadata": {"email": body.email, "deviceId": body.deviceId, "product": body.product},
        },
        success_url="https://materialcheck.elektrogenius.de/upgrade-success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://materialcheck.elektrogenius.de",
        locale="de",
    )
    return {"url": session.url}


class PortalSessionRequest(BaseModel):
    email: str


@api_router.post("/stripe/create-portal-session")
async def create_portal_session(body: PortalSessionRequest):
    if not _stripe_available or not stripe_lib:
        raise HTTPException(status_code=503, detail="Stripe nicht konfiguriert. Bitte STRIPE_SECRET_KEY auf Render.com setzen.")
    profile = await db.profiles.find_one({"email": norm_email(body.email)})
    customer_id = profile.get("stripeCustomerId") if profile else None
    if not customer_id:
        raise HTTPException(status_code=404, detail="Kein Stripe-Konto gefunden. Bitte zuerst ein Abo abschließen.")
    try:
        portal = stripe_lib.billing_portal.Session.create(
            customer=customer_id,
            return_url="https://materialcheck.elektrogenius.de",
        )
        return {"url": portal.url}
    except Exception as e:
        logging.error(f"[portal] Stripe-Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def generate_order_number() -> str:
    year = datetime.utcnow().year
    prefix = f"EG-{year}-"
    last = await db.orders.find_one(
        {"orderNumber": {"$regex": f"^{prefix}"}},
        sort=[("orderNumber", -1)],
    )
    if last:
        try:
            seq = int(last["orderNumber"].split("-")[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


@api_router.post("/stripe/webhook")
async def stripe_webhook(req: Request):
    if not _stripe_available or not stripe_lib:
        raise HTTPException(status_code=503, detail="Stripe nicht konfiguriert. Bitte STRIPE_SECRET_KEY auf Render.com setzen.")
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET nicht gesetzt")

    body = (await req.body()).decode("utf-8")
    sig = req.headers.get("stripe-signature", "")

    try:
        event = stripe_lib.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
    except stripe_lib.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Webhook Signatur ungültig")
    except Exception as e:
        logging.error(f"Webhook Fehler: {e}")
        raise HTTPException(status_code=400, detail="Webhook konnte nicht verarbeitet werden")

    try:
        if event["type"] == "checkout.session.completed":
            print("[Webhook] checkout.session.completed erreicht")
            # Trial startet: subscription ist jetzt "trialing"
            session_obj = event["data"]["object"]
            email = (
                (session_obj.get("customer_details") or {}).get("email")
                or session_obj.get("customer_email")
                or (session_obj.get("metadata") or {}).get("email")
            )
            if not email:
                print("[Webhook] Keine Email gefunden – Abbruch")
                return {"received": True}
            sub_id = session_obj.get("subscription")
            in_trial = False
            trial_ends_at = None
            if sub_id and stripe_lib:
                try:
                    sub = stripe_lib.Subscription.retrieve(sub_id)
                    sub_status = sub.get("status") if hasattr(sub, "get") else getattr(sub, "status", None)
                    trial_end_ts = sub.get("trial_end") if hasattr(sub, "get") else getattr(sub, "trial_end", None)
                    in_trial = sub_status == "trialing"
                    if trial_end_ts:
                        trial_ends_at = datetime.utcfromtimestamp(trial_end_ts)
                except Exception as sub_err:
                    logging.warning(f"Subscription abruf fehlgeschlagen: {sub_err}")
            update_fields: dict = {
                "isPremium": True,
                "premiumSince": datetime.utcnow(),
                "inTrial": in_trial,
            }
            if trial_ends_at:
                update_fields["trialEndsAt"] = trial_ends_at
            await db.profiles.update_one(
                {"email": norm_email(email)},
                {"$set": update_fields},
            )
            status_label = "Trial gestartet" if in_trial else "aktiviert"
            logging.info(f"✅ MaterialCheck+ {status_label} für {email}")

            # Bestellung speichern
            product = (session_obj.get("metadata") or {}).get("product", "materialcheck-plus")
            amount = session_obj.get("amount_total", 0)
            print(f"[Resend] Vorbereitung Email {email} | Produkt: {product} | Betrag: {amount}")
            order_number = await generate_order_number()
            await db.orders.insert_one({
                "orderNumber": order_number,
                "email": norm_email(email),
                "product": product,
                "amount": amount,
                "createdAt": datetime.utcnow(),
            })
            print(f"[DB] Bestellung gespeichert: {order_number}")
            logging.info(f"📦 Bestellung {order_number} gespeichert für {email}")

            # Bestätigungs-E-Mail senden
            if not _resend_available or not resend_lib:
                logging.warning("[Resend] Übersprungen – RESEND_API_KEY nicht gesetzt oder Paket fehlt")
            else:
                try:
                    print(f"[Resend] Sende E-Mail an: {email}")
                    response = resend_lib.Emails.send({
                        "from": "ElektroGenius <info@elektrogenius.de>",
                        "to": [email],
                        "subject": f"Bestellbestätigung {order_number}",
                        "html": f"""
                        <h2>Danke für deine Bestellung!</h2>
                        <p><strong>Bestellnummer:</strong> {order_number}</p>
                        <p><strong>Produkt:</strong> {product}</p>
                        <p>Wir melden uns in Kürze bei dir.</p>
                        """,
                    })
                    print(f"[Resend] Response: {response}")
                    logging.info(f"📧 Bestätigungs-E-Mail gesendet an {email} | ID: {getattr(response, 'id', response)}")
                except Exception as mail_err:
                    print(f"[Resend] Fehler: {mail_err}")
                    logging.error(f"[Resend] E-Mail-Versand fehlgeschlagen für {email}: {mail_err}")

        elif event["type"] == "customer.subscription.deleted":
            subscription = event["data"]["object"]
            email = (subscription.get("metadata") or {}).get("email")
            if email:
                await db.profiles.update_one(
                    {"email": norm_email(email)},
                    {"$set": {"isPremium": False, "inTrial": False, "trialEndsAt": None}},
                )
                logging.info(f"❌ MaterialCheck+ deaktiviert für {email}")

        elif event["type"] == "customer.subscription.updated":
            subscription = event["data"]["object"]
            email = (subscription.get("metadata") or {}).get("email")
            status = subscription.get("status")
            # "trialing" und "active" gelten als premium
            is_active = status in ("active", "trialing")
            in_trial = status == "trialing"
            trial_end_ts = subscription.get("trial_end")
            if email:
                update_fields = {
                    "isPremium": is_active,
                    "inTrial": in_trial,
                }
                if trial_end_ts:
                    update_fields["trialEndsAt"] = datetime.utcfromtimestamp(trial_end_ts)
                elif not in_trial:
                    update_fields["trialEndsAt"] = None
                await db.profiles.update_one(
                    {"email": norm_email(email)},
                    {"$set": update_fields},
                )
                logging.info(f"🔄 MaterialCheck+ Status={status} für {email}")

        elif event["type"] == "invoice.payment_succeeded":
            # Wiederkehrende Zahlung erfolgreich → isPremium sicherstellen
            invoice = event["data"]["object"]
            customer_id = invoice.get("customer")
            # Nur für Abonnement-Rechnungen (nicht Einmal-Zahlungen)
            if customer_id and invoice.get("subscription"):
                result = await db.profiles.update_one(
                    {"stripeCustomerId": customer_id},
                    {"$set": {"isPremium": True}},
                )
                if result.modified_count:
                    logging.info(f"💳 Zahlung erfolgreich – isPremium=True für Stripe-Kunde {customer_id}")

        elif event["type"] == "invoice.payment_failed":
            # Zahlung fehlgeschlagen → Zugang sperren
            invoice = event["data"]["object"]
            customer_id = invoice.get("customer")
            if customer_id and invoice.get("subscription"):
                result = await db.profiles.update_one(
                    {"stripeCustomerId": customer_id},
                    {"$set": {"isPremium": False}},
                )
                if result.modified_count:
                    logging.info(f"❌ Zahlung fehlgeschlagen – isPremium=False für Stripe-Kunde {customer_id}")

        elif event["type"] == "customer.subscription.trial_will_end":
            # 3 Tage vor Trial-Ende — optional: hier könnte eine E-Mail verschickt werden
            subscription = event["data"]["object"]
            email = (subscription.get("metadata") or {}).get("email")
            logging.info(f"⏰ Trial endet bald für {email}")

    except Exception as e:
        logging.error(f"Webhook Verarbeitung Fehler: {e}")

    return {"received": True}


# -----------------------------
# ROUTER + MIDDLEWARE
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
