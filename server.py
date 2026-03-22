from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

import os, logging, hashlib, json, uuid
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime
import re
import uvicorn

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()
api_router = APIRouter(prefix="/api")

# ─── WEBSOCKET MANAGER ────────────────────────────
class ConnectionManager:
    def __init__(self):
        # folder_id -> list of websockets
        self.active: Dict[str, List[WebSocket]] = {}
        # websocket -> user info
        self.users: Dict[WebSocket, dict] = {}

    async def connect(self, ws: WebSocket, folder_id: str, user_info: dict):
        await ws.accept()
        if folder_id not in self.active:
            self.active[folder_id] = []
        self.active[folder_id].append(ws)
        self.users[ws] = { **user_info, "folder_id": folder_id, "connectedAt": datetime.utcnow().isoformat() }
        await self.broadcast_active_users(folder_id)

    def disconnect(self, ws: WebSocket):
        folder_id = self.users.get(ws, {}).get("folder_id")
        if folder_id and folder_id in self.active:
            self.active[folder_id] = [w for w in self.active[folder_id] if w != ws]
        self.users.pop(ws, None)
        return folder_id

    async def broadcast_to_folder(self, folder_id: str, message: dict, exclude: WebSocket = None):
        if folder_id not in self.active:
            return
        dead = []
        for ws in self.active[folder_id]:
            if ws == exclude:
                continue
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_active_users(self, folder_id: str):
        if folder_id not in self.active:
            return
        users = [self.users[ws] for ws in self.active[folder_id] if ws in self.users]
        await self.broadcast_to_folder(folder_id, { "type": "active_users", "users": users })

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

class ProfileLoadRequest(BaseModel):
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
    folderId: str
    folderName: str
    role: str = "member"  # "member" | "readonly"

class AcceptInviteRequest(BaseModel):
    inviteId: str
    userEmail: str
    deviceId: str

class GrantRoleRequest(BaseModel):
    companyId: str
    ownerEmail: str
    targetEmail: str
    folderId: str
    newRole: str  # "member" | "readonly" | "admin"

class FolderSyncRequest(BaseModel):
    companyId: str
    folderId: str
    userEmail: str
    materials: list = []
    activities: list = []
    syncedAt: str = ""

class MaterialSyncModel(BaseModel):
    deviceId: str
    email: str
    folders: list = []
    materials: list = []
    tasks: list = []
    suppliers: list = []
    loans: list = []
    syncedAt: str = ""

class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"

class ChatResponse(BaseModel):
    response: str
    actions_taken: List[str] = []

# ─── HELPERS ──────────────────────────────────────

def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()

def new_id() -> str:
    return str(uuid.uuid4())

# ─── HEALTH ───────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

@api_router.get("/")
async def root():
    return {"message": "MaterialCheck API v2 — Team Edition"}

# ─── PROFIL ───────────────────────────────────────

@api_router.post("/profile")
async def save_profile(profile: ProfileModel):
    try:
        data = profile.model_dump()
        if data.get("pin"):
            data["pinHash"] = hash_pin(data["pin"])
        data.pop("pin", None)
        await db.profiles.update_one({"deviceId": profile.deviceId}, {"$set": data}, upsert=True)
        return {"success": True}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.post("/profile/load")
async def load_profile(request: ProfileLoadRequest):
    try:
        if not request.pin or len(request.pin) != 6:
            raise HTTPException(status_code=400, detail="PIN muss 6 Stellen haben")
        profile = await db.profiles.find_one({"email": request.email})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        if hash_pin(request.pin) != profile.get("pinHash", ""):
            raise HTTPException(status_code=401, detail="Falscher PIN")
        profile.pop("_id", None); profile.pop("pinHash", None); profile.pop("pin", None)
        return profile
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Fehler")

@api_router.get("/profile/check/{email}")
async def check_profile(email: str):
    profile = await db.profiles.find_one({"email": email})
    if not profile:
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    return {"exists": True, "hasPin": bool(profile.get("pinHash")), "firmName": profile.get("firmName",""), "userName": profile.get("userName","")}

@api_router.get("/profile/{device_id}")
async def get_profile(device_id: str):
    profile = await db.profiles.find_one({"deviceId": device_id})
    if not profile:
        raise HTTPException(status_code=404, detail="Nicht gefunden")
    profile.pop("_id", None); profile.pop("pinHash", None)
    return profile

# ─── MATERIALIEN SYNC (Privat) ────────────────────

@api_router.post("/materials/sync")
async def sync_materials(data: MaterialSyncModel):
    try:
        sync_data = data.model_dump()
        sync_data["syncedAt"] = datetime.utcnow().isoformat()
        await db.material_syncs.update_one({"email": data.email}, {"$set": sync_data}, upsert=True)
        return {"success": True, "syncedAt": sync_data["syncedAt"]}
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Sync Fehler")

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
        raise HTTPException(status_code=500, detail="Fehler")

# ─── FIRMA ERSTELLEN ──────────────────────────────

@api_router.post("/company/create")
async def create_company(req: CompanyCreateRequest):
    try:
        # Prüfen ob Nutzer existiert
        profile = await db.profiles.find_one({"email": req.ownerEmail})
        if not profile:
            raise HTTPException(status_code=404, detail="Profil nicht gefunden — zuerst Profil erstellen")

        # Firma erstellen
        company_id = new_id()
        company = {
            "companyId": company_id,
            "companyName": req.companyName,
            "ownerEmail": req.ownerEmail,
            "ownerName": req.ownerName,
            "createdAt": datetime.utcnow().isoformat(),
            "members": [{
                "email": req.ownerEmail,
                "name": req.ownerName,
                "role": "owner",
                "joinedAt": datetime.utcnow().isoformat(),
                "deviceId": req.deviceId,
                "isActive": False,
            }],
            "folders": [],
        }
        await db.companies.insert_one(company)

        # Profil updaten
        await db.profiles.update_one({"email": req.ownerEmail}, {"$set": {"companyId": company_id, "companyRole": "owner"}})

        return {"success": True, "companyId": company_id, "companyName": req.companyName}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

# ─── FIRMA LADEN ──────────────────────────────────

@api_router.get("/company/{company_id}")
async def get_company(company_id: str, email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        company.pop("_id", None)

        # Prüfen ob Nutzer Mitglied ist
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        if not member:
            raise HTTPException(status_code=403, detail="Kein Zugriff")

        # Nur Ordner zurückgeben auf die der Nutzer Zugriff hat
        user_role = member.get("role", "readonly")
        if user_role in ["owner", "admin"]:
            # Chef/Admin sieht alle Ordner
            accessible_folders = company.get("folders", [])
        else:
            # Mitarbeiter sieht nur eingeladene Ordner
            accessible_folders = [f for f in company.get("folders", [])
                                 if any(a["email"] == email for a in f.get("access", []))]

        return {
            "companyId": company["companyId"],
            "companyName": company["companyName"],
            "ownerEmail": company["ownerEmail"],
            "members": company.get("members", []),
            "folders": accessible_folders,
            "userRole": user_role,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── EINLADUNG SENDEN ─────────────────────────────

@api_router.post("/company/invite")
async def invite_member(req: InviteRequest):
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")

        # Nur Chef/Admin darf einladen
        inviter = next((m for m in company.get("members", []) if m["email"] == req.inviterEmail), None)
        if not inviter or inviter.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin kann einladen")

        # Einladung erstellen
        invite_id = new_id()
        invite = {
            "inviteId": invite_id,
            "companyId": req.companyId,
            "companyName": company["companyName"],
            "inviterEmail": req.inviterEmail,
            "inviterName": inviter.get("name", ""),
            "inviteeEmail": req.inviteeEmail,
            "folderId": req.folderId,
            "folderName": req.folderName,
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

# ─── EINLADUNGEN ABRUFEN ──────────────────────────

@api_router.get("/company/invites/{email}")
async def get_invites(email: str):
    try:
        invites = await db.invitations.find({"inviteeEmail": email, "status": "pending"}).to_list(50)
        for inv in invites:
            inv.pop("_id", None)
        return invites
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── EINLADUNG ANNEHMEN ───────────────────────────

@api_router.post("/company/accept")
async def accept_invite(req: AcceptInviteRequest):
    try:
        invite = await db.invitations.find_one({"inviteId": req.inviteId})
        if not invite:
            raise HTTPException(status_code=404, detail="Einladung nicht gefunden")
        if invite["inviteeEmail"] != req.userEmail:
            raise HTTPException(status_code=403, detail="Nicht berechtigt")
        if invite["status"] != "pending":
            raise HTTPException(status_code=400, detail="Einladung bereits bearbeitet")

        company_id = invite["companyId"]
        company = await db.companies.find_one({"companyId": company_id})

        # Nutzer als Mitglied hinzufügen falls noch nicht
        existing = next((m for m in company.get("members", []) if m["email"] == req.userEmail), None)
        if not existing:
            profile = await db.profiles.find_one({"email": req.userEmail})
            new_member = {
                "email": req.userEmail,
                "name": profile.get("userName","") if profile else req.userEmail,
                "role": invite["role"],
                "joinedAt": datetime.utcnow().isoformat(),
                "deviceId": req.deviceId,
                "isActive": False,
            }
            await db.companies.update_one({"companyId": company_id}, {"$push": {"members": new_member}})

        # Ordner Zugriff hinzufügen
        folder_id = invite["folderId"]
        folders = company.get("folders", [])
        folder_exists = any(f["folderId"] == folder_id for f in folders)

        if not folder_exists:
            # Ordner in Firma erstellen
            await db.companies.update_one({"companyId": company_id}, {"$push": {"folders": {
                "folderId": folder_id,
                "folderName": invite["folderName"],
                "access": [{"email": req.userEmail, "role": invite["role"]}],
                "materials": [],
                "activities": [],
                "lastSync": datetime.utcnow().isoformat(),
            }}})
        else:
            # Nutzer zu Ordner Zugriff hinzufügen
            await db.companies.update_one(
                {"companyId": company_id, "folders.folderId": folder_id},
                {"$push": {"folders.$.access": {"email": req.userEmail, "role": invite["role"]}}}
            )

        # Einladung als angenommen markieren
        await db.invitations.update_one({"inviteId": req.inviteId}, {"$set": {"status": "accepted"}})

        # Profil updaten
        await db.profiles.update_one({"email": req.userEmail}, {"$set": {"companyId": company_id, "companyRole": invite["role"]}})

        return {"success": True, "companyId": company_id, "folderId": folder_id}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

# ─── ROLLEN ÄNDERN ────────────────────────────────

@api_router.post("/company/role")
async def change_role(req: GrantRoleRequest):
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        owner = next((m for m in company.get("members", []) if m["email"] == req.ownerEmail), None)
        if not owner or owner.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin")

        # Rolle ändern
        await db.companies.update_one(
            {"companyId": req.companyId, "members.email": req.targetEmail},
            {"$set": {"members.$.role": req.newRole}}
        )
        # Ordner Zugriff updaten
        await db.companies.update_one(
            {"companyId": req.companyId, "folders.folderId": req.folderId, "folders.access.email": req.targetEmail},
            {"$set": {"folders.$[f].access.$[a].role": req.newRole}},
            array_filters=[{"f.folderId": req.folderId}, {"a.email": req.targetEmail}]
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── FIRMA ORDNER SYNC ────────────────────────────

@api_router.post("/company/folder/sync")
async def sync_company_folder(req: FolderSyncRequest):
    try:
        company = await db.companies.find_one({"companyId": req.companyId})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")

        # Zugriff prüfen
        folders = company.get("folders", [])
        folder = next((f for f in folders if f["folderId"] == req.folderId), None)
        if not folder:
            raise HTTPException(status_code=404, detail="Ordner nicht gefunden")

        user_access = next((a for a in folder.get("access", []) if a["email"] == req.userEmail), None)
        member = next((m for m in company.get("members", []) if m["email"] == req.userEmail), None)
        if not user_access and not (member and member.get("role") in ["owner", "admin"]):
            raise HTTPException(status_code=403, detail="Kein Zugriff")

        # Readonly darf nicht schreiben
        if user_access and user_access.get("role") == "readonly":
            raise HTTPException(status_code=403, detail="Nur Lesezugriff")

        now = datetime.utcnow().isoformat()
        await db.companies.update_one(
            {"companyId": req.companyId, "folders.folderId": req.folderId},
            {"$set": {
                "folders.$.materials": req.materials,
                "folders.$.activities": req.activities,
                "folders.$.lastSync": now,
                "folders.$.lastSyncBy": req.userEmail,
            }}
        )
        # WebSocket broadcast
        await manager.broadcast_to_folder(req.folderId, {
            "type": "folder_updated",
            "folderId": req.folderId,
            "updatedBy": req.userEmail,
            "materials": req.materials,
            "activities": req.activities,
            "syncedAt": now,
        })
        return {"success": True, "syncedAt": now}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail=str(e))

# ─── FIRMA ORDNER LADEN ───────────────────────────

@api_router.get("/company/{company_id}/folder/{folder_id}")
async def get_company_folder(company_id: str, folder_id: str, email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        if not company:
            raise HTTPException(status_code=404, detail="Firma nicht gefunden")
        folder = next((f for f in company.get("folders", []) if f["folderId"] == folder_id), None)
        if not folder:
            raise HTTPException(status_code=404, detail="Ordner nicht gefunden")
        # Zugriff prüfen
        member = next((m for m in company.get("members", []) if m["email"] == email), None)
        access = next((a for a in folder.get("access", []) if a["email"] == email), None)
        if not access and not (member and member.get("role") in ["owner", "admin"]):
            raise HTTPException(status_code=403, detail="Kein Zugriff")
        return {
            "folderId": folder["folderId"],
            "folderName": folder.get("folderName", ""),
            "materials": folder.get("materials", []),
            "activities": folder.get("activities", []),
            "lastSync": folder.get("lastSync", ""),
            "lastSyncBy": folder.get("lastSyncBy", ""),
            "userRole": access.get("role", "member") if access else member.get("role", "owner"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── MITGLIED ENTFERNEN ───────────────────────────

@api_router.delete("/company/{company_id}/member/{email}")
async def remove_member(company_id: str, email: str, owner_email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
        owner = next((m for m in company.get("members", []) if m["email"] == owner_email), None)
        if not owner or owner.get("role") not in ["owner", "admin"]:
            raise HTTPException(status_code=403, detail="Nur Chef oder Admin")
        await db.companies.update_one({"companyId": company_id}, {"$pull": {"members": {"email": email}}})
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── AI CHAT ──────────────────────────────────────

@api_router.post("/chat", response_model=ChatResponse)
async def chat_with_ai(request: ChatRequest):
    system_message = """Du bist ein KI-Assistent für eine Materiallager App für Elektriker.
Beantworte Fragen zu Elektrotechnik, Materialien, VDE-Normen und Installationen.
Antworte immer auf Deutsch, klar und verständlich."""
    try:
        completion = await ai_client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=300, temperature=0.3,
            messages=[{"role":"system","content":system_message},{"role":"user","content":request.message}],
        )
        return ChatResponse(response=completion.choices[0].message.content, actions_taken=[])
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="KI Fehler")

# ─── WEBSOCKET ────────────────────────────────────

@app.websocket("/ws/folder/{folder_id}")
async def websocket_folder(ws: WebSocket, folder_id: str, email: str = "", name: str = ""):
    user_info = {"email": email, "name": name or email}
    await manager.connect(ws, folder_id, user_info)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")
            if msg_type == "ping":
                await ws.send_json({"type": "pong"})
            elif msg_type == "material_change":
                # Sofort an alle anderen broadcasten
                await manager.broadcast_to_folder(folder_id, {
                    "type": "material_change",
                    "materialId": data.get("materialId"),
                    "qty": data.get("qty"),
                    "changedBy": email,
                    "changedByName": name,
                    "timestamp": datetime.utcnow().isoformat(),
                }, exclude=ws)
            elif msg_type == "typing":
                await manager.broadcast_to_folder(folder_id, {
                    "type": "user_active",
                    "email": email,
                    "name": name,
                }, exclude=ws)
    except WebSocketDisconnect:
        folder_id = manager.disconnect(ws)
        if folder_id:
            await manager.broadcast_to_folder(folder_id, {
                "type": "user_left",
                "email": email,
                "name": name,
            })
            await manager.broadcast_active_users(folder_id)

# ─── ROUTER ───────────────────────────────────────

app.include_router(api_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("shutdown")
async def shutdown():
    client.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
