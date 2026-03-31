from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

import os
import logging
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
                    "from": "MaterialCheck <noreply@elektrogenius.de>",
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
    email: str

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
            action_data = json.loads(json_match.group(0))
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
        # Hash PIN if provided in plaintext (6 chars or less = plain)
        if data.get("pin") and len(data["pin"]) <= 6:
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
            await db.profiles.update_one({"email": data["email"]}, {"$set": data}, upsert=True)
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
        await db.profiles.update_one({"email": req.email}, {"$set": {"deviceId": req.deviceId}})
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
    """Materialien in Cloud speichern"""
    try:
        data = {
            "deviceId": req.deviceId,
            "email": norm_email(req.email) if req.email else req.email,
            "folders": req.folders,
            "materials": req.materials,
            "tasks": req.tasks,
            "suppliers": req.suppliers,
            "loans": req.loans,
            "syncedAt": req.syncedAt or datetime.utcnow().isoformat(),
        }
        # Upsert by email (primary) or deviceId
        filter_q = {"email": req.email} if req.email else {"deviceId": req.deviceId}
        await db.materials_sync.update_one(filter_q, {"$set": data}, upsert=True)
        return {"success": True, "message": f"{len(req.materials)} Materialien gespeichert"}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Sync fehlgeschlagen")

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
    """Firma erstellen"""
    try:
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
        company = await db.companies.find_one({"ownerEmail": email})
        if not company:
            # Check if member
            company = await db.companies.find_one({"members.email": email})
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
    """Firma verlassen"""
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        if company.get("ownerEmail") == email:
            raise HTTPException(status_code=400, detail="Owner kann Firma nicht verlassen — Firma löschen")
        await db.companies.update_one(
            {"companyId": company_id},
            {"$pull": {"members": {"email": email}}}
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
                member_emails = [m.get("email") for m in company.get("members", [])]
                if email not in member_emails:
                    raise HTTPException(status_code=403, detail="Kein Zugriff")
        sync_data = await db.warehouse_materials.find_one({
            "companyId": company_id, "warehouseId": warehouse_id
        })
        if not sync_data:
            return {"materials": [], "syncedAt": ""}
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
        member = next((m for m in company.get("members", []) if m["email"] == req.email), None)
        if not member:
            raise HTTPException(status_code=403, detail="Kein Zugriff")
        data = {
            "companyId": req.companyId,
            "warehouseId": req.warehouseId,
            "materials": req.materials,
            "syncedAt": req.syncedAt or datetime.utcnow().isoformat(),
            "syncedBy": req.email,
        }
        await db.warehouse_materials.update_one(
            {"companyId": req.companyId, "warehouseId": req.warehouseId},
            {"$set": data}, upsert=True
        )
        await broadcast_company_update(req.companyId, {
            "type": "warehouse_synced",
            "warehouseId": req.warehouseId,
            "by": req.email,
            "count": len(req.materials),
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
            except Exception:
                pass
    except WebSocketDisconnect:
        if company_id in company_connections:
            try:
                company_connections[company_id].remove(websocket)
            except ValueError:
                pass

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
