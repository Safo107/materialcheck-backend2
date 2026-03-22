from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI

import os, logging, hashlib, json, uuid
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime
import re, uvicorn

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]
ai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

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
    return {"message": "MaterialCheck API v3 — Warehouse Edition"}

# ─── PROFIL ───────────────────────────────────────

@api_router.post("/profile")
async def save_profile(profile: ProfileModel):
    try:
        data = profile.model_dump()
        if data.get("pin"):
            data["pinHash"] = hash_pin(data["pin"])
        data.pop("pin", None)
        # Aktives Gerät tracken — nur 1 aktives Gerät pro Email
        data["lastActiveAt"] = datetime.utcnow().isoformat()
        await db.profiles.update_one(
            {"deviceId": profile.deviceId},
            {"$set": data},
            upsert=True
        )
        # Andere Geräte mit gleicher Email als inaktiv markieren
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
        profile = await db.profiles.find_one({"email": request.email})
        if not profile:
            raise HTTPException(status_code=404, detail="Kein Profil gefunden")
        if hash_pin(request.pin) != profile.get("pinHash", ""):
            raise HTTPException(status_code=401, detail="Falscher PIN")
        # Altes Gerät abmelden, neues aktivieren
        if request.deviceId:
            await db.profiles.update_many(
                {"email": request.email},
                {"$set": {"isActive": False}}
            )
            # Neues Gerät registrieren
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
    profile = await db.profiles.find_one({"email": email})
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
            "warehouses": [],  # Geteilte Lager
        }
        await db.companies.insert_one(company)
        await db.profiles.update_one(
            {"email": req.ownerEmail},
            {"$set": {"companyId": company_id, "companyRole": "owner"}}
        )
        return {"success": True, "companyId": company_id, "companyName": req.companyName}
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
        # Prüfen ob Eingeladener ein Konto hat
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

@api_router.get("/company/invites/{email}")
async def get_invites(email: str):
    try:
        invites = await db.invitations.find({"inviteeEmail": email, "status": "pending"}).to_list(50)
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
                "materials": [],
                "tasks": [],
                "activities": [],
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
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_router.delete("/company/{company_id}/member/{email}")
async def remove_member(company_id: str, email: str, owner_email: str):
    try:
        company = await db.companies.find_one({"companyId": company_id})
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

# ─── LAGER SYNC (Echtzeit) ────────────────────────

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
        # Echtzeit broadcast
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

# ─── LAGER ERSTELLEN (Chef) ───────────────────────

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
            "materials": [],
            "tasks": [],
            "activities": [],
            "lastSync": datetime.utcnow().isoformat(),
            "lastSyncBy": email,
        }
        await db.companies.update_one({"companyId": company_id}, {"$push": {"warehouses": warehouse}})
        return {"success": True, "warehouseId": wh_id}
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
                # Sofort an alle anderen im Lager senden
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
