from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

import os, logging, hashlib, json, uuid, random, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime, timedelta
import re, uvicorn

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]
ai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# Gmail SMTP Konfiguration
GMAIL_USER = os.environ.get("GMAIL_USER", "safindeler10@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ─── WEBSOCKET MANAGER ────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, List[WebSocket]] = {}
        self.users: Dict[WebSocket, dict] = {}

    async def connect(self, ws: WebSocket, room: str, user_info: dict):
        await ws.accept()
        if room not in self.active:
            self.active[room] = []
        self.active[room].append(ws)
        self.users[ws] = {**user_info, "room": room, "connectedAt": datetime.utcnow().isoformat()}
        await self.broadcast_active_users(room)

    def disconnect(self, ws: WebSocket):
        room = self.users.get(ws, {}).get("room")
        if room and room in self.active:
            self.active[room] = [w for w in self.active[room] if w != ws]
        self.users.pop(ws, None)
        return room

    async def broadcast(self, room: str, message: dict, exclude: WebSocket = None):
        if room not in self.active:
            return
        dead = []
        for ws in self.active[room]:
            if ws == exclude:
                continue
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_active_users(self, room: str):
        if room not in self.active:
            return
        users = [self.users[ws] for ws in self.active[room] if ws in self.users]
        await self.broadcast(room, {"type": "active_users", "users": users})

manager = ConnectionManager()

# ─── MODELS ───────────────────────────────────────

class ProfileModel(BaseModel):
    deviceId: str
    firmName: str = ""
    userName: str = ""
    email: str = ""
    logoUri: Optional[str] = None
    updatedAt: str = ""
    pin: Optional[str] = None
    companyId: Optional[str] = None
    companyRole: Optional[str] = None

class ProfileLoadRequest(BaseModel):
    email: str
    pin: str
    deviceId: str = ""

class MaterialSyncModel(BaseModel):
    deviceId: str
    email: str
    folders: list = []
    materials: list = []
    tasks: list = []
    suppliers: list = []
    loans: list = []
    syncedAt: str = ""

class CompanyCreateRequest(BaseModel):
    ownerEmail: str
    ownerName: str
    companyName: str
    deviceId: str

class InviteRequest(BaseModel):
    companyId: str
    inviterEmail: str
    inviteeEmail: str
    warehouseId: str
    warehouseName: str
    role: str = "member"

class AcceptInviteRequest(BaseModel):
    inviteId: str
    userEmail: str
    deviceId: str

class GrantRoleRequest(BaseModel):
    companyId: str
    ownerEmail: str
    targetEmail: str
    warehouseId: str
    newRole: str

class WarehouseSyncRequest(BaseModel):
    companyId: str
    warehouseId: str
    userEmail: str
    materials: list = []
    tasks: list = []
    activities: list = []
    syncedAt: str = ""

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"

class ChatResponse(BaseModel):
    response: str
    actions_taken: List[str] = []

class PinResetRequest(BaseModel):
    email: str

class PinResetConfirmRequest(BaseModel):
    email: str
    code: str
    newPin: str
    deviceId: str = ""

# ─── HELPERS ──────────────────────────────────────

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def new_id() -> str:
    return str(uuid.uuid4())

def send_reset_email(to_email: str, code: str, firm_name: str = "") -> bool:
    """Send PIN reset code via Gmail SMTP"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🔐 MaterialCheck — PIN zurücksetzen"
        msg["From"] = GMAIL_USER
        msg["To"] = to_email

        name = firm_name or to_email
        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background: #0d1117; color: #e6edf3; padding: 20px;">
            <div style="max-width: 500px; margin: 0 auto; background: #161b22; border-radius: 16px; padding: 30px; border: 1px solid rgba(255,255,255,0.1);">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 48px;">⚡</div>
                    <h1 style="color: #f5a623; margin: 8px 0; font-size: 24px;">MaterialCheck</h1>
                    <p style="color: #8b949e; font-size: 14px;">von ElektroGenius</p>
                </div>
                <h2 style="color: #e6edf3; font-size: 18px;">Hallo {name}!</h2>
                <p style="color: #8b949e; line-height: 1.6;">Du hast einen PIN-Reset angefordert. Hier ist dein 6-stelliger Reset-Code:</p>
                <div style="background: #21262d; border: 2px solid #f5a623; border-radius: 12px; padding: 24px; text-align: center; margin: 24px 0;">
                    <div style="font-size: 42px; font-weight: bold; letter-spacing: 12px; color: #f5a623;">{code}</div>
                    <p style="color: #8b949e; font-size: 12px; margin-top: 12px;">Gültig für 15 Minuten</p>
                </div>
                <p style="color: #8b949e; font-size: 13px; line-height: 1.6;">Falls du keinen Reset angefordert hast, kannst du diese Email ignorieren. Dein PIN bleibt unverändert.</p>
                <div style="border-top: 1px solid rgba(255,255,255,0.1); margin-top: 24px; padding-top: 16px; text-align: center;">
                    <p style="color: #6e7681; font-size: 11px;">MaterialCheck · ElektroGenius · elektrogenius.de</p>
                </div>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        logging.error(f"Email send error: {e}")
        return False

# ─── HEALTH ───────────────────────────────────────

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}

@api_router.get("/")
async def root():
    return {"message": "MaterialCheck API v3 — Warehouse Edition"}

# ─── PROFIL ───────────────────────────────────────

@api_router.post("/profile")
async def save_profile(profile: ProfileModel):
    try:
        data = profile.model_dump()
        if data.get("pin"):
            data["pinHash"] = hash_pin(data["pin"])
        data.pop("pin", None)
        data["lastActiveAt"] = datetime.utcnow().isoformat()
        await db.profiles.update_one(
            {"deviceId": profile.deviceId},
            {"$set": data},
            upsert=True
        )
        if profile.email:
            await db.profiles.update_many(
                {"email": profile.email, "deviceId": {"$ne": profile.deviceId}},
                {"$set": {"isActive": False}}
            )
            await db.profiles.update_one(
                {"deviceId": profile.deviceId},
                {"$set": {"isActive": True}}
            )
        return {"success": True}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/profile/load")
async def load_profile(request: ProfileLoadRequest):
    try:
        if not request.pin or len(request.pin) != 6:
            raise HTTPException(status_code=400, detail="PIN muss 6 Stellen haben")
        profile = await db.profiles.find_one(
            {"email": {"$regex": f"^{re.escape(request.email)}$", "$options": "i"}}
        )
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        if hash_pin(request.pin) != profile.get("pinHash", ""):
            raise HTTPException(status_code=401, detail="Falscher PIN")
        if request.deviceId:
            await db.profiles.update_many(
                {"email": request.email},
                {"$set": {"isActive": False}}
            )
            profile.pop("_id", None)
            new_profile = {**profile, "deviceId": request.deviceId, "isActive": True, "lastActiveAt": datetime.utcnow().isoformat()}
            await db.profiles.update_one(
                {"deviceId": request.deviceId},
                {"$set": new_profile},
                upsert=True
            )
        profile.pop("_id", None)
        profile.pop("pinHash", None)
        profile.pop("pin", None)
        return profile
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.get("/profile/check/{email}")
async def check_profile(email: str):
    profile = await db.profiles.find_one(
        {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}}
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    return {
        "exists": True,
        "hasPin": bool(profile.get("pinHash")),
        "firmName": profile.get("firmName", ""),
        "userName": profile.get("userName", ""),
        "companyId": profile.get("companyId", ""),
        "companyRole": profile.get("companyRole", ""),
    }

@api_router.get("/profile/{device_id}")
async def get_profile(device_id: str):
    profile = await db.profiles.find_one({"deviceId": device_id})
    if not profile:
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    profile.pop("_id", None)
    profile.pop("pinHash", None)
    return profile

# ─── PIN RESET ────────────────────────────────────

@api_router.post("/profile/reset-pin/request")
async def request_pin_reset(req: PinResetRequest):
    """Schickt einen 6-stelligen Reset-Code per Email"""
    try:
        profile = await db.profiles.find_one(
            {"email": {"$regex": f"^{re.escape(req.email)}$", "$options": "i"}}
        )
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil mit dieser Email gefunden")

        # 6-stelligen Code generieren
        code = str(random.randint(100000, 999999))
        expires_at = (datetime.utcnow() + timedelta(minutes=15)).isoformat()

        # Code in DB speichern
        await db.pin_resets.update_one(
            {"email": req.email.lower()},
            {"$set": {"code": hash_pin(code), "expiresAt": expires_at, "used": False}},
            upsert=True
        )

        # Email senden
        firm_name = profile.get("firmName", "") or profile.get("userName", "")
        success = send_reset_email(req.email, code, firm_name)

        if not success:
            raise HTTPException(status_code=500, detail="Email konnte nicht gesendet werden")

        return {"success": True, "message": "Reset-Code wurde per Email gesendet"}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/profile/reset-pin/confirm")
async def confirm_pin_reset(req: PinResetConfirmRequest):
    """Bestätigt den Reset-Code und setzt neuen PIN"""
    try:
        if not req.newPin or len(req.newPin) != 6:
            raise HTTPException(status_code=400, detail="Neuer PIN muss 6 Stellen haben")

        reset = await db.pin_resets.find_one({"email": req.email.lower()})
        if not reset:
            raise HTTPException(status_code=404, detail="Kein Reset angefordert")

        if reset.get("used"):
            raise HTTPException(status_code=400, detail="Code bereits verwendet")

        expires_at = datetime.fromisoformat(reset["expiresAt"])
        if datetime.utcnow() > expires_at:
            raise HTTPException(status_code=400, detail="Code abgelaufen — bitte neu anfordern")

        if hash_pin(req.code) != reset.get("code"):
            raise HTTPException(status_code=401, detail="Falscher Code")

        # Neuen PIN setzen
        new_pin_hash = hash_pin(req.newPin)
        await db.profiles.update_many(
            {"email": {"$regex": f"^{re.escape(req.email)}$", "$options": "i"}},
            {"$set": {"pinHash": new_pin_hash}}
        )

        # Code als verwendet markieren
        await db.pin_resets.update_one(
            {"email": req.email.lower()},
            {"$set": {"used": True}}
        )

        # Profil laden und zurückgeben
        profile = await db.profiles.find_one(
            {"email": {"$regex": f"^{re.escape(req.email)}$", "$options": "i"}}
        )
        if profile and req.deviceId:
            new_profile = {**profile, "deviceId": req.deviceId, "isActive": True}
            await db.profiles.update_one(
                {"deviceId": req.deviceId},
                {"$set": new_profile},
                upsert=True
            )

        if profile:
            profile.pop("_id", None)
            profile.pop("pinHash", None)
            profile.pop("pin", None)
            return {"success": True, "profile": profile}

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

# ─── MATERIALIEN SYNC (Privat) ────────────────────

@api_router.post("/materials/sync")
async def sync_materials(data: MaterialSyncModel):
    try:
        sync_data = data.model_dump()
        sync_data["syncedAt"] = datetime.utcnow().isoformat()
        await db.material_syncs.update_one({"email": data.email}, {"$set": sync_data}, upsert=True)
        return {"success": True, "syncedAt": sync_data["syncedAt"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/materials/sync/{email}")
async def get_synced_materials(email: str, pin: str):
    try:
        if not pin or len(pin) != 6:
            raise HTTPException(status_code=400, detail="PIN erforderlich")
        profile = await db.profiles.find_one({"email": email})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil")
        if hash_pin(pin) != profile.get("pinHash", ""):
            raise HTTPException(status_code=401, detail="Falscher PIN")
        sync = await db.material_syncs.find_one({"email": email})
        if not sync:
            raise HTTPException(status_code=404, detail="Keine Daten")
        sync.pop("_id", None)
        return sync
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── FIRMA ────────────────────────────────────────

@api_router.post("/company/create")
async def create_company(req: CompanyCreateRequest):
    try:
        profile = await db.profiles.find_one({"email": req.ownerEmail})
        if not profile:
            raise HTTPException(status_code=404, detail="Profil nicht gefunden — zuerst Profil in Cloud speichern")

        existing_owner = await db.companies.find_one({"ownerEmail": req.ownerEmail})
        if existing_owner:
            return {
                "success": True,
                "companyId": existing_owner["companyId"],
                "companyName": existing_owner["companyName"],
                "alreadyExists": True
            }

        name_taken = await db.companies.find_one({
            "companyName": {"$regex": f"^{req.companyName.strip()}$", "$options": "i"}
        })
        if name_taken:
            raise HTTPException(status_code=409, detail=f'Der Firmenname "{req.companyName}" ist bereits vergeben.')

        company_id = new_id()
        company = {
            "companyId": company_id,
            "companyName": req.companyName,
            "ownerEmail": req.ownerEmail,
            "createdAt": datetime.utcnow().isoformat(),
            "members": [{
                "email": req.ownerEmail,
                "name": req.ownerName,
                "role": "owner",
                "joinedAt": datetime.utcnow().isoformat(),
                "deviceId": req.deviceId,
                "isActive": True,
            }],
            "warehouses": [],
        }
        await db.companies.insert_one(company)
        await db.profiles.update_one(
            {"email": req.ownerEmail},
            {"$set": {"companyId": company_id, "companyRole": "owner"}}
        )
        return {"success": True, "companyId": company_id, "companyName": req.companyName, "alreadyExists": False}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/company/by-owner/{email}")
async def get_company_by_owner(email: str):
    try:
        company = await db.companies.find_one({"ownerEmail": email})
        if not company:
            company = await db.companies.find_one({"members.email": email})
        if not company:
            raise HTTPException(status_code=404, detail="Keine Firma gefunden")
        company.pop("_id", None)
        return {
            "companyId": company["companyId"],
            "companyName": company["companyName"],
            "ownerEmail": company["ownerEmail"],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/company/{company_id}")
async def get_company(company_id: str, email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        company.pop("_id", None)
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        if not member:
            raise HTTPException(status_code=403, detail="Kein Zugriff")
        user_role = member.get("role", "member")
        if user_role in ["owner", "admin"]:
            warehouses = company.get("warehouses", [])
        else:
            warehouses = [w for w in company.get("warehouses", [])
                         if any(a["email"] == email for a in w.get("access", []))]
        return {
            "companyId": company["companyId"],
            "companyName": company["companyName"],
            "ownerEmail": company["ownerEmail"],
            "members": company.get("members", []),
            "warehouses": warehouses,
            "userRole": user_role,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── EINLADUNGEN ──────────────────────────────────

@api_router.post("/company/invite")
async def invite_member(req: InviteRequest):
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        inviter = next((m for m in company.get("members", []) if m["email"] == req.inviterEmail), None)
        if not inviter or inviter.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin kann einladen")
        invitee_profile = await db.profiles.find_one({"email": req.inviteeEmail})
        if not invitee_profile:
            raise HTTPException(status_code=404, detail=f"Kein Konto mit Email {req.inviteeEmail} gefunden")
        invite_id = new_id()
        invite = {
            "inviteId": invite_id,
            "companyId": req.companyId,
            "companyName": company["companyName"],
            "inviterEmail": req.inviterEmail,
            "inviterName": inviter.get("name", ""),
            "inviteeEmail": req.inviteeEmail,
            "warehouseId": req.warehouseId,
            "warehouseName": req.warehouseName,
            "role": req.role,
            "status": "pending",
            "createdAt": datetime.utcnow().isoformat(),
        }
        await db.invitations.insert_one(invite)
        return {"success": True, "inviteId": invite_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/company/reject")
async def reject_invite(request: dict):
    try:
        invite_id = request.get("inviteId", "")
        if not invite_id:
            raise HTTPException(status_code=400, detail="inviteId fehlt")
        await db.invitations.update_one(
            {"inviteId": invite_id},
            {"$set": {"status": "rejected"}}
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/company/{company_id}/leave")
async def leave_company(company_id: str, email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            return {"success": True}
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        if member and member.get("role") == "owner":
            raise HTTPException(status_code=403, detail="Chef kann die Firma nicht verlassen.")
        await db.companies.update_one(
            {"companyId": company_id},
            {"$pull": {"members": {"email": email}}}
        )
        await db.profiles.update_one(
            {"email": email},
            {"$unset": {"companyId": "", "companyRole": ""}}
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/company/invites/{email}")
async def get_invites(email: str):
    try:
        invites = await db.invitations.find({
            "inviteeEmail": email,
            "status": "pending"
        }).to_list(50)
        for inv in invites:
            inv.pop("_id", None)
        return invites
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/company/accept")
async def accept_invite(req: AcceptInviteRequest):
    try:
        invite = await db.invitations.find_one({"inviteId": req.inviteId})
        if not invite:
            raise HTTPException(status_code=404, detail="Einladung nicht gefunden")
        if invite["inviteeEmail"] != req.userEmail:
            raise HTTPException(status_code=403, detail="Nicht berechtigt")
        if invite["status"] != "pending":
            raise HTTPException(status_code=400, detail="Bereits bearbeitet")
        company_id = invite["companyId"]
        company = await db.companies.find_one({"companyId": company_id})
        existing = next((m for m in company.get("members", []) if m["email"] == req.userEmail), None)
        if not existing:
            profile = await db.profiles.find_one({"email": req.userEmail})
            new_member = {
                "email": req.userEmail,
                "name": profile.get("userName", "") if profile else req.userEmail,
                "role": invite["role"],
                "joinedAt": datetime.utcnow().isoformat(),
                "deviceId": req.deviceId,
                "isActive": True,
            }
            await db.companies.update_one({"companyId": company_id}, {"$push": {"members": new_member}})
        warehouse_id = invite["warehouseId"]
        warehouses = company.get("warehouses", [])
        wh_exists = any(w["warehouseId"] == warehouse_id for w in warehouses)
        if not wh_exists:
            await db.companies.update_one({"companyId": company_id}, {"$push": {"warehouses": {
                "warehouseId": warehouse_id,
                "warehouseName": invite["warehouseName"],
                "warehouseIcon": "🏗️",
                "access": [{"email": req.userEmail, "role": invite["role"]}],
                "materials": [], "tasks": [], "activities": [],
                "lastSync": datetime.utcnow().isoformat(),
                "lastSyncBy": req.userEmail,
            }}})
        else:
            await db.companies.update_one(
                {"companyId": company_id, "warehouses.warehouseId": warehouse_id},
                {"$push": {"warehouses.$.access": {"email": req.userEmail, "role": invite["role"]}}}
            )
        await db.invitations.update_one({"inviteId": req.inviteId}, {"$set": {"status": "accepted"}})
        await db.profiles.update_one(
            {"email": req.userEmail},
            {"$set": {"companyId": company_id, "companyRole": invite["role"]}}
        )
        return {"success": True, "companyId": company_id, "warehouseId": warehouse_id}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/company/role")
async def change_role(req: GrantRoleRequest):
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        owner = next((m for m in company.get("members", []) if m["email"] == req.ownerEmail), None)
        if not owner or owner.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin")
        await db.companies.update_one(
            {"companyId": req.companyId, "members.email": req.targetEmail},
            {"$set": {"members.$.role": req.newRole}}
        )
        await db.profiles.update_one(
            {"email": req.targetEmail},
            {"$set": {"companyRole": req.newRole}}
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/company/{company_id}/member/{email}")
async def remove_member(company_id: str, email: str, owner_email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        owner = next((m for m in company.get("members", []) if m["email"] == owner_email), None)
        if not owner or owner.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin")
        await db.companies.update_one({"companyId": company_id}, {"$pull": {"members": {"email": email}}})
        await db.profiles.update_one({"email": email}, {"$unset": {"companyId": "", "companyRole": ""}})
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── LAGER SYNC ───────────────────────────────────

@api_router.post("/company/warehouse/sync")
async def sync_warehouse(req: WarehouseSyncRequest):
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        warehouses = company.get("warehouses", [])
        wh = next((w for w in warehouses if w["warehouseId"] == req.warehouseId), None)
        if not wh:
            raise HTTPException(status_code=404, detail="Lager nicht gefunden")
        member = next((m for m in company.get("members", []) if m["email"] == req.userEmail), None)
        access = next((a for a in wh.get("access", []) if a["email"] == req.userEmail), None)
        if not access and not (member and member.get("role") in ["owner", "admin"]):
            raise HTTPException(status_code=403, detail="Kein Zugriff")
        if access and access.get("role") == "readonly":
            raise HTTPException(status_code=403, detail="Nur Lesezugriff")
        now = datetime.utcnow().isoformat()
        await db.companies.update_one(
            {"companyId": req.companyId, "warehouses.warehouseId": req.warehouseId},
            {"$set": {
                "warehouses.$.materials": req.materials,
                "warehouses.$.tasks": req.tasks,
                "warehouses.$.activities": req.activities,
                "warehouses.$.lastSync": now,
                "warehouses.$.lastSyncBy": req.userEmail,
            }}
        )
        await manager.broadcast(req.warehouseId, {
            "type": "warehouse_updated",
            "warehouseId": req.warehouseId,
            "updatedBy": req.userEmail,
            "materials": req.materials,
            "tasks": req.tasks,
            "activities": req.activities,
            "syncedAt": now,
        })
        return {"success": True, "syncedAt": now}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/company/{company_id}/warehouse/{warehouse_id}")
async def get_warehouse(company_id: str, warehouse_id: str, email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        wh = next((w for w in company.get("warehouses", []) if w["warehouseId"] == warehouse_id), None)
        if not wh:
            raise HTTPException(status_code=404, detail="Lager nicht gefunden")
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        access = next((a for a in wh.get("access", []) if a["email"] == email), None)
        if not access and not (member and member.get("role") in ["owner", "admin"]):
            raise HTTPException(status_code=403, detail="Kein Zugriff")
        return {
            "warehouseId": wh["warehouseId"],
            "warehouseName": wh.get("warehouseName", ""),
            "warehouseIcon": wh.get("warehouseIcon", "🏗️"),
            "materials": wh.get("materials", []),
            "tasks": wh.get("tasks", []),
            "activities": wh.get("activities", []),
            "lastSync": wh.get("lastSync", ""),
            "lastSyncBy": wh.get("lastSyncBy", ""),
            "access": wh.get("access", []),
            "userRole": access.get("role", "member") if access else member.get("role", "owner"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/company/{company_id}/warehouse/create")
async def create_warehouse(company_id: str, email: str, name: str, icon: str = "🏗️"):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        if not member or member.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin")
        wh_id = new_id()
        warehouse = {
            "warehouseId": wh_id,
            "warehouseName": name,
            "warehouseIcon": icon,
            "access": [{"email": email, "role": "owner"}],
            "materials": [], "tasks": [], "activities": [],
            "lastSync": datetime.utcnow().isoformat(),
            "lastSyncBy": email,
        }
        await db.companies.update_one({"companyId": company_id}, {"$push": {"warehouses": warehouse}})
        return {"success": True, "warehouseId": wh_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/company/{company_id}/warehouse/{warehouse_id}")
async def delete_warehouse(company_id: str, warehouse_id: str, email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        if not member or member.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin")
        await db.companies.update_one(
            {"companyId": company_id},
            {"$pull": {"warehouses": {"warehouseId": warehouse_id}}}
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── AI CHAT ──────────────────────────────────────

@api_router.post("/chat", response_model=ChatResponse)
async def chat_with_ai(request: ChatRequest):
    system_message = "Du bist ein KI-Assistent für eine Materiallager App für Elektriker. Antworte auf Deutsch, klar und verständlich."
    try:
        completion = await ai_client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=300, temperature=0.3,
            messages=[{"role":"system","content":system_message},{"role":"user","content":request.message}],
        )
        return ChatResponse(response=completion.choices[0].message.content, actions_taken=[])
    except Exception as e:
        raise HTTPException(status_code=500, detail="KI Fehler")

# ─── WEBSOCKET ────────────────────────────────────

@app.websocket("/ws/warehouse/{warehouse_id}")
async def websocket_warehouse(ws: WebSocket, warehouse_id: str, email: str = "", name: str = ""):
    user_info = {"email": email, "name": name or email}
    await manager.connect(ws, warehouse_id, user_info)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")
            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
            elif msg_type == "material_change":
                await manager.broadcast(warehouse_id, {
                    "type": "material_change",
                    "materialId": data.get("materialId"),
                    "qty": data.get("qty"),
                    "changedBy": email,
                    "changedByName": name,
                    "timestamp": datetime.utcnow().isoformat(),
                }, exclude=ws)
            elif msg_type == "task_change":
                await manager.broadcast(warehouse_id, {
                    "type": "task_change",
                    "taskId": data.get("taskId"),
                    "status": data.get("status"),
                    "changedBy": email,
                }, exclude=ws)
    except WebSocketDisconnect:
        room = manager.disconnect(ws)
        if room:
            await manager.broadcast(room, {"type": "user_left", "email": email, "name": name})
            await manager.broadcast_active_users(room)

# ─── ROUTER ───────────────────────────────────────

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("shutdown")
async def shutdown():
    client.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
