#!/usr/bin/env python3
"""
oci_grab.py - Capte une instance Oracle Cloud "Always Free" des qu'une machine
se libere, sans se faire rate-limiter.

Strategie (c'est ce qui change tout par rapport a un simple retry en boucle) :

  1. On interroge l'API "compute capacity report" d'Oracle, qui dit si une shape
     est disponible dans une AD/fault domain SANS consommer le quota de requetes
     LaunchInstance. On peut donc sonder souvent (60 s) au lieu de tenter a
     l'aveugle toutes les 5 min.
  2. Des qu'un emplacement repond AVAILABLE, on lance immediatement - on arrive
     dans la seconde ou la capacite apparait, pas 5 minutes apres.
  3. On balaye toutes les availability domains ET tous les fault domains : la
     capacite apparait souvent dans un seul FD.
  4. On essaie une "echelle" de configurations, de la plus grosse a la plus
     petite (4 OCPU/24 GB -> 2/12 -> 1/6), et en option un fallback AMD micro.
     Une petite config se trouve beaucoup plus vite.
  5. On calcule le quota free tier restant pour ne jamais demander plus que ce
     a quoi on a droit (et donc ne pas se faire rejeter en LimitExceeded).
  6. Rate limiting maison : espacement minimum garanti entre appels, backoff
     exponentiel avec respect de Retry-After, jitter partout.

Usage:
    python oci_grab.py discover         # affiche les OCIDs + l'etat de capacite
    python oci_grab.py probe            # un seul scan de capacite, sans rien creer
    python oci_grab.py run              # boucle jusqu'a obtenir la machine
    python oci_grab.py run --once       # un cycle borne (cron / GitHub Actions)
    python oci_grab.py run --dry-run    # tout sauf le LaunchInstance final
"""

from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import oci

# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #

_T0 = time.monotonic()


def log(msg: str, level: str = "info") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    tag = {"info": "   ", "ok": " ok", "warn": "  !", "err": " xx"}.get(level, "   ")
    print(f"[{ts}Z{tag}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Noms alternatifs acceptes, pour rester compatible avec les secrets
# deja en place sur d'autres scripts Oracle.
ALIASES = {
    "OCI_USER_OCID": ("OCI_USER",),
    "OCI_TENANCY_OCID": ("OCI_TENANCY",),
    "OCI_KEY_CONTENT": ("OCI_KEY", "OCI_PRIVATE_KEY"),
    "OCI_SUBNET_OCID": ("OCI_SUBNET",),
    "OCI_IMAGE_OCID": ("OCI_IMAGE",),
    "OCI_COMPARTMENT_OCID": ("OCI_COMPARTMENT",),
    "OCI_SSH_PUBLIC_KEY": ("SSH_PUBKEY", "OCI_SSH_KEY"),
}


EXIT_CONFIG = 6


def die_config(msg: str):
    """Sortie distincte de 'pas de capacite' (1), pour que la CI passe au rouge."""
    print(f"[config] {msg}", file=sys.stderr, flush=True)
    sys.exit(EXIT_CONFIG)


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name)
    if not val:
        for alias in ALIASES.get(name, ()):
            val = os.environ.get(alias)
            if val:
                break
    if not val:
        val = default
    if val is not None:
        val = val.strip()
    if required and not val:
        die_config(f"variable d'environnement manquante : {name}")
    return val


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env(name, str(default))))
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    return (env(name, "true" if default else "false") or "").lower() in ("1", "true", "yes", "y")


# Quotas "Always Free" Oracle (par tenancy, dans la home region).
FREE_A1_OCPUS = 4
FREE_A1_MEMORY_GB = 24
FREE_MICRO_COUNT = 2
A1_SHAPE = "VM.Standard.A1.Flex"
MICRO_SHAPE = "VM.Standard.E2.1.Micro"


@dataclass(frozen=True)
class Target:
    """Une configuration de machine a tenter."""
    shape: str
    ocpus: float
    memory_gb: float

    @property
    def is_flex(self) -> bool:
        return self.shape.endswith(".Flex")

    def __str__(self) -> str:
        if self.is_flex:
            return f"{self.shape} {self.ocpus:g}c/{self.memory_gb:g}GB"
        return self.shape


@dataclass
class Settings:
    compartment_id: str = ""
    subnet_id: str | None = None
    image_id: str | None = None
    display_name: str = "free-arm-1"
    ssh_key: str = ""
    boot_volume_gb: int = 50
    assign_public_ip: bool = True
    ad_filter: list[str] = field(default_factory=list)

    shape: str = A1_SHAPE
    ocpus: float = 4
    memory_gb: float = 24
    ladder: bool = True
    micro_fallback: bool = False

    probe_enabled: bool = True
    probe_interval: int = 60
    launch_interval: int = 300
    jitter: int = 30
    max_backoff: int = 3600
    max_attempts: int = 0
    max_runtime: int = 0
    once_cycles: int = 0

    tg_token: str = ""
    tg_chat: str = ""
    webhook: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        st = cls()
        st.compartment_id = env("OCI_COMPARTMENT_OCID") or env("OCI_TENANCY_OCID", required=True)
        st.subnet_id = env("OCI_SUBNET_OCID")
        st.image_id = env("OCI_IMAGE_OCID")
        st.display_name = env("OCI_DISPLAY_NAME", "free-arm-1")
        st.ssh_key = env("OCI_SSH_PUBLIC_KEY", "") or ""
        st.boot_volume_gb = env_int("OCI_BOOT_VOLUME_GB", 50)
        st.assign_public_ip = env_bool("OCI_ASSIGN_PUBLIC_IP", True)
        st.ad_filter = [a.strip() for a in (env("OCI_AVAILABILITY_DOMAINS", "") or "").split(",") if a.strip()]

        st.shape = env("OCI_SHAPE", A1_SHAPE)
        st.ocpus = float(env("OCI_OCPUS", "4"))
        st.memory_gb = float(env("OCI_MEMORY_GB", "24"))
        st.ladder = env_bool("TRY_SMALLER_SHAPES", True)
        st.micro_fallback = env_bool("FALLBACK_TO_MICRO", False)

        st.probe_enabled = env_bool("CAPACITY_PROBE", True)
        st.probe_interval = max(20, env_int("PROBE_INTERVAL_SECONDS", 60))
        st.launch_interval = max(60, env_int("RETRY_INTERVAL_SECONDS", 300))
        st.jitter = max(0, env_int("RETRY_JITTER_SECONDS", 30))
        st.max_backoff = env_int("MAX_BACKOFF_SECONDS", 3600)
        st.max_attempts = env_int("MAX_ATTEMPTS", 0)
        st.max_runtime = env_int("MAX_RUNTIME_SECONDS", 0)

        st.tg_token = env("TELEGRAM_BOT_TOKEN", "") or ""
        st.tg_chat = env("TELEGRAM_CHAT_ID", "") or ""
        st.webhook = env("WEBHOOK_URL", "") or ""
        return st

    def targets(self) -> list[Target]:
        """L'echelle de configurations a tenter, de la plus grosse a la plus petite."""
        out = [Target(self.shape, self.ocpus, self.memory_gb)]
        if self.ladder and self.shape == A1_SHAPE:
            # memes ratios que le quota free (6 GB par OCPU)
            for ocpus in (3, 2, 1):
                if ocpus < self.ocpus:
                    out.append(Target(A1_SHAPE, ocpus, min(self.memory_gb, ocpus * 6)))
        if self.micro_fallback and self.shape != MICRO_SHAPE:
            out.append(Target(MICRO_SHAPE, 1, 1))
        return out


def build_oci_config() -> dict:
    """Config OCI depuis l'environnement, sans fichier ~/.oci/config."""
    cfg = {
        "user": env("OCI_USER_OCID", required=True),
        "fingerprint": env("OCI_FINGERPRINT", required=True),
        "tenancy": env("OCI_TENANCY_OCID", required=True),
        "region": env("OCI_REGION", required=True),
    }
    key = env("OCI_KEY_CONTENT")
    if key:
        if "-----BEGIN" not in key:
            key = base64.b64decode(key).decode()       # pratique pour les secrets CI
        cfg["key_content"] = key.replace("\\n", "\n")
    else:
        cfg["key_file"] = env("OCI_KEY_FILE", required=True)
    passphrase = env("OCI_KEY_PASSPHRASE")
    if passphrase:
        cfg["pass_phrase"] = passphrase
    oci.config.validate_config(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def notify(st: Settings, text: str) -> None:
    """Best effort : une notif qui echoue ne doit jamais casser la boucle."""
    if not (st.tg_token or st.webhook):
        return
    try:
        import requests
    except ImportError:
        return
    try:
        if st.tg_token and st.tg_chat:
            requests.post(
                f"https://api.telegram.org/bot{st.tg_token}/sendMessage",
                json={"chat_id": st.tg_chat, "text": text},
                timeout=20,
            )
        if st.webhook:
            requests.post(st.webhook, json={"text": text}, timeout=20)
    except Exception as exc:  # noqa: BLE001
        log(f"notification echouee : {exc}", "warn")


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

class Pacer:
    """Garantit un espacement minimum entre appels d'un meme type d'API."""

    def __init__(self, min_spacing: float):
        self.min_spacing = min_spacing
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_spacing:
            time.sleep(self.min_spacing - delta)
        self._last = time.monotonic()


def sleep_until(deadline: float, label: str) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        log(f"pause {int(remaining)}s ({label})")
        time.sleep(remaining)


# --------------------------------------------------------------------------- #
# Classification des erreurs
# --------------------------------------------------------------------------- #

CAPACITY_CODES = {"OutOfCapacity", "OutOfHostCapacity", "InternalError"}
QUOTA_CODES = {"LimitExceeded", "QuotaExceeded"}

CAPACITY = "capacity"
RATELIMIT = "ratelimit"
QUOTA = "quota"
AUTH = "auth"
BADCONF = "badconf"
FATAL = "fatal"


def classify(err: oci.exceptions.ServiceError) -> str:
    code = err.code or ""
    msg = (err.message or "").lower()

    if err.status == 429 or code == "TooManyRequests":
        return RATELIMIT
    if code in QUOTA_CODES or ("limit" in msg and "exceed" in msg):
        return QUOTA
    if err.status in (401, 403):
        return AUTH
    if "capacity" in msg or code in CAPACITY_CODES or err.status in (500, 502, 503, 504):
        return CAPACITY
    if err.status in (400, 404, 409):
        # shape/image incompatibles, subnet introuvable... inutile de reessayer tel quel
        return BADCONF
    return FATAL


def retry_after(err, fallback: int) -> int:
    try:
        headers = err.headers or {}
        for key in ("retry-after", "Retry-After"):
            if key in headers:
                return max(1, int(float(headers[key])))
    except Exception:  # noqa: BLE001
        pass
    return fallback


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class Grabber:
    def __init__(self, cfg: dict, st: Settings):
        self.cfg = cfg
        self.st = st
        self.compute = oci.core.ComputeClient(cfg)
        self.identity = oci.identity.IdentityClient(cfg)
        self.network = oci.core.VirtualNetworkClient(cfg)
        self.probe_pacer = Pacer(2.0)
        self.launch_pacer = Pacer(5.0)
        self.probe_supported = st.probe_enabled
        self.attempts = 0
        self.stats: dict[str, int] = {}
        self._image_cache: dict[str, str | None] = {}
        self._fd_cache: dict[str, list[str]] = {}

    # ----- inventaire ----------------------------------------------------- #

    def availability_domains(self) -> list[str]:
        ads = [ad.name for ad in self.identity.list_availability_domains(
            compartment_id=self.cfg["tenancy"]).data]
        if self.st.ad_filter:
            wanted = {w.lower() for w in self.st.ad_filter}
            kept = [ad for ad in ads if ad.lower() in wanted or any(w in ad.lower() for w in wanted)]
            if kept:
                return kept
            log(f"OCI_AVAILABILITY_DOMAINS ne matche rien, on garde les {len(ads)} ADs", "warn")
        return ads

    def fault_domains(self, ad: str) -> list[str]:
        """Les FD a essayer. None en premier = laisse Oracle choisir."""
        if ad not in self._fd_cache:
            try:
                fds = [fd.name for fd in self.identity.list_fault_domains(
                    compartment_id=self.cfg["tenancy"], availability_domain=ad).data]
            except oci.exceptions.ServiceError:
                fds = []
            self._fd_cache[ad] = fds
        return self._fd_cache[ad]

    def resolve_subnet(self) -> str:
        if self.st.subnet_id:
            return self.st.subnet_id
        subnets = self.network.list_subnets(compartment_id=self.st.compartment_id).data
        public = [s for s in subnets if not s.prohibit_public_ip_on_vnic]
        chosen = (public or subnets)
        if not chosen:
            die_config("aucun subnet trouve : cree un VCN avec un subnet public d'abord")
        log(f"subnet auto-detecte : {chosen[0].display_name}")
        self.st.subnet_id = chosen[0].id
        return chosen[0].id

    def resolve_image(self, shape: str) -> str:
        """Image par shape : ARM et AMD n'utilisent pas les memes images."""
        if self.st.image_id and shape == self.st.shape:
            return self.st.image_id
        if shape in self._image_cache and self._image_cache[shape]:
            return self._image_cache[shape]  # type: ignore[return-value]
        for os_name in ("Canonical Ubuntu", "Oracle Linux"):
            try:
                imgs = self.compute.list_images(
                    compartment_id=self.st.compartment_id,
                    operating_system=os_name,
                    shape=shape,
                    sort_by="TIMECREATED",
                    sort_order="DESC",
                    limit=1,
                ).data
            except oci.exceptions.ServiceError:
                continue
            if imgs:
                log(f"image auto-detectee pour {shape} : {imgs[0].display_name}")
                self._image_cache[shape] = imgs[0].id
                return imgs[0].id
        die_config(f"aucune image trouvee pour {shape} : renseigne OCI_IMAGE_OCID")

    # ----- quota ---------------------------------------------------------- #

    def quota_left(self) -> tuple[float, float, int]:
        """(ocpus A1 restants, GB A1 restants, micros restantes) d'apres l'existant."""
        used_oc = used_mem = 0.0
        micros = 0
        try:
            instances = self.compute.list_instances(compartment_id=self.st.compartment_id).data
        except oci.exceptions.ServiceError as exc:
            log(f"impossible de lister les instances ({exc.code}), quota suppose intact", "warn")
            return FREE_A1_OCPUS, FREE_A1_MEMORY_GB, FREE_MICRO_COUNT
        for inst in instances:
            if inst.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            if inst.shape == A1_SHAPE:
                sc = inst.shape_config
                used_oc += getattr(sc, "ocpus", 0) or 0
                used_mem += getattr(sc, "memory_in_gbs", 0) or 0
            elif inst.shape == MICRO_SHAPE:
                micros += 1
        return (FREE_A1_OCPUS - used_oc, FREE_A1_MEMORY_GB - used_mem, FREE_MICRO_COUNT - micros)

    def existing_instance(self):
        try:
            instances = self.compute.list_instances(compartment_id=self.st.compartment_id).data
        except oci.exceptions.ServiceError:
            return None
        for inst in instances:
            if inst.display_name == self.st.display_name and inst.lifecycle_state not in (
                "TERMINATED", "TERMINATING"
            ):
                return inst
        return None

    def usable_targets(self) -> list[Target]:
        """Filtre l'echelle selon le quota free tier reellement disponible."""
        oc_left, mem_left, micro_left = self.quota_left()
        log(f"quota free restant : A1 {oc_left:g} OCPU / {mem_left:g} GB, micro x{micro_left}")
        out = []
        for t in self.st.targets():
            if t.shape == A1_SHAPE:
                if t.ocpus <= oc_left and t.memory_gb <= mem_left:
                    out.append(t)
            elif t.shape == MICRO_SHAPE:
                if micro_left > 0:
                    out.append(t)
            else:
                out.append(t)  # shape non-free : on ne sait pas, on laisse passer
        return out

    # ----- sonde de capacite ---------------------------------------------- #

    def probe(self, ad: str, target: Target) -> list[str | None]:
        """
        Fault domains ou la capacite est disponible, le plus fourni en premier.
        [] = rien de libre. None dans la liste = AD sans FD precis.

        Un seul appel couvre tous les fault domains, et n'entame pas le quota
        LaunchInstance : c'est tout l'interet.
        """
        if not self.probe_supported:
            return []
        models = oci.core.models

        def entry(fd: str | None):
            sa = models.CreateCapacityReportShapeAvailabilityDetails(instance_shape=target.shape)
            if fd:
                sa.fault_domain = fd
            if target.is_flex:
                sa.instance_shape_config = models.CapacityReportInstanceShapeConfig(
                    ocpus=target.ocpus, memory_in_gbs=target.memory_gb
                )
            return sa

        fds = self.fault_domains(ad)
        entries = [entry(fd) for fd in fds] if fds else [entry(None)]

        try:
            details = models.CreateComputeCapacityReportDetails(
                compartment_id=self.st.compartment_id,
                availability_domain=ad,
                shape_availabilities=entries,
            )
            self.probe_pacer.wait()
            report = self.compute.create_compute_capacity_report(details).data
        except oci.exceptions.ServiceError as exc:
            if classify(exc) == RATELIMIT:
                raise
            log(f"sonde de capacite indisponible ({exc.code}), passage en mode aveugle", "warn")
            self.probe_supported = False
            return []
        except Exception as exc:  # SDK trop ancien, modele absent...  # noqa: BLE001
            log(f"sonde de capacite non supportee ({type(exc).__name__}), mode aveugle", "warn")
            self.probe_supported = False
            return []

        found: list[tuple[int, str | None]] = []
        for av in report.shape_availabilities or []:
            if (getattr(av, "availability_status", "") or "").upper() == "AVAILABLE":
                count = getattr(av, "available_count", None) or 0
                found.append((count, getattr(av, "fault_domain", None)))
        found.sort(key=lambda item: -item[0])        # le FD le plus fourni d'abord
        return [fd for _, fd in found]

    # ----- lancement ------------------------------------------------------ #

    def launch_details(self, ad: str, fd: str | None, target: Target):
        models = oci.core.models
        details = models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=self.st.compartment_id,
            shape=target.shape,
            display_name=self.st.display_name,
            source_details=models.InstanceSourceViaImageDetails(
                image_id=self.resolve_image(target.shape),
                boot_volume_size_in_gbs=self.st.boot_volume_gb,
            ),
            create_vnic_details=models.CreateVnicDetails(
                subnet_id=self.resolve_subnet(),
                assign_public_ip=self.st.assign_public_ip,
            ),
            metadata={"ssh_authorized_keys": self.st.ssh_key} if self.st.ssh_key else {},
        )
        if fd:
            details.fault_domain = fd
        if target.is_flex:
            details.shape_config = models.LaunchInstanceShapeConfigDetails(
                ocpus=target.ocpus, memory_in_gbs=target.memory_gb
            )
        return details

    def launch(self, ad: str, fd: str | None, target: Target, dry_run: bool):
        """-> ('success', instance) | (statut, ServiceError)"""
        self.attempts += 1
        where = f"{ad.split(':')[-1]}{'/' + fd.split('-')[-1] if fd else ''}"
        if dry_run:
            log(f"[dry-run] LaunchInstance {target} dans {where}", "ok")
            return "dryrun", None
        try:
            self.launch_pacer.wait()
            resp = self.compute.launch_instance(
                self.launch_details(ad, fd, target),
                retry_strategy=oci.retry.NoneRetryStrategy(),
            )
            return "success", resp.data
        except oci.exceptions.ServiceError as exc:
            kind = classify(exc)
            self.stats[kind] = self.stats.get(kind, 0) + 1
            return kind, exc


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #

def cmd_discover() -> int:
    cfg = build_oci_config()
    st = Settings.from_env()
    g = Grabber(cfg, st)

    print(f"\nregion       : {cfg['region']}")
    print(f"tenancy      : {cfg['tenancy']}")
    print(f"compartiment : {st.compartment_id}\n")

    print("--- availability domains (OCI_AVAILABILITY_DOMAINS) ---")
    ads = g.availability_domains()
    for ad in ads:
        fds = g.fault_domains(ad)
        print(f"  {ad}    fault domains: {len(fds)}")

    print("\n--- subnets (OCI_SUBNET_OCID) ---")
    try:
        for sn in g.network.list_subnets(compartment_id=st.compartment_id).data:
            kind = "prive " if sn.prohibit_public_ip_on_vnic else "public"
            print(f"  [{kind}] {sn.display_name:<26} {sn.id}")
    except oci.exceptions.ServiceError as exc:
        print(f"  erreur : {exc.code} - {exc.message}")

    print("\n--- images recentes (OCI_IMAGE_OCID) ---")
    for shape in {st.shape, A1_SHAPE, MICRO_SHAPE}:
        for os_name in ("Canonical Ubuntu",):
            try:
                imgs = g.compute.list_images(
                    compartment_id=st.compartment_id, operating_system=os_name,
                    shape=shape, sort_by="TIMECREATED", sort_order="DESC", limit=2,
                ).data
            except oci.exceptions.ServiceError:
                continue
            for img in imgs:
                print(f"  {shape:<26} {img.display_name:<42} {img.id}")

    oc, mem, micro = g.quota_left()
    print(f"\n--- quota Always Free restant ---")
    print(f"  A1.Flex : {oc:g} OCPU / {mem:g} GB")
    print(f"  E2.1.Micro : {micro} instance(s)")

    print("\n--- etat de capacite maintenant ---")
    cmd_probe(g, ads)
    return 0


def cmd_probe(g: Grabber | None = None, ads: list[str] | None = None) -> int:
    if g is None:
        g = Grabber(build_oci_config(), Settings.from_env())
    if ads is None:
        ads = g.availability_domains()
    targets = g.st.targets()
    found = False
    for target in targets:
        for ad in ads:
            try:
                available = g.probe(ad, target)
            except oci.exceptions.ServiceError as exc:
                log(f"rate limit sur la sonde : {exc.code}", "warn")
                return 1
            if not g.probe_supported:
                log("sonde non disponible sur ce tenancy/SDK", "warn")
                return 1
            mark = "DISPONIBLE" if available else "plein"
            suffix = " (fd " + ", ".join(fd.split("-")[-1] if fd else "ad" for fd in available) + ")" \
                if available else ""
            log(f"{str(target):<32} {ad.split(':')[-1]:<18} {mark}{suffix}",
                "ok" if available else "info")
            found = found or bool(available)
    return 0 if found else 1


def cycle(g: Grabber, ads: list[str], targets: list[Target], dry_run: bool):
    """
    Un passage complet. Retourne (code_de_sortie | None, instance | None).
    None = rien trouve, on continue.
    """
    st = g.st
    blind = not g.probe_supported

    for target in targets:
        for ad in random.sample(ads, len(ads)):   # evite de toujours taper la meme AD en premier
            fds: list[str | None] = [None]

            if not blind:
                try:
                    available = g.probe(ad, target)
                except oci.exceptions.ServiceError as exc:
                    wait = retry_after(exc, 120)
                    log(f"rate limit sur la sonde -> pause {wait}s", "warn")
                    time.sleep(wait)
                    continue
                if g.probe_supported:
                    if not available:
                        log(f"{str(target):<32} {ad.split(':')[-1]:<18} plein")
                        continue
                    where = ", ".join(fd.split("-")[-1] if fd else "ad" for fd in available)
                    log(f"CAPACITE DISPO : {target} dans {ad} (fd {where})", "ok")
                    fds = available
                else:
                    blind = True

            if blind:
                # mode aveugle : on tente aussi les fault domains un par un,
                # la capacite est souvent dans un seul FD
                fds = [None] + list(g.fault_domains(ad))

            for fd in fds:
                status, payload = g.launch(ad, fd, target, dry_run)

                if status == "success":
                    return 0, payload
                if status == "dryrun":
                    continue

                err = payload
                label = f"#{g.attempts} {str(target):<28} {ad.split(':')[-1]}"

                if status == CAPACITY:
                    log(f"{label} : pas de capacite ({err.status} {err.code})")
                    continue
                if status == RATELIMIT:
                    wait = retry_after(err, min(st.launch_interval * 2, st.max_backoff))
                    log(f"{label} : RATE LIMIT 429 -> pause {wait}s", "warn")
                    time.sleep(min(wait, st.max_backoff))
                    continue
                if status == QUOTA:
                    log(f"{label} : quota atteint ({err.code}) - {err.message}", "err")
                    notify(st, f"Oracle : quota free tier atteint ({err.code}). Arret.")
                    return 3, None
                if status == AUTH:
                    log(f"{label} : auth/permissions ({err.code}) - {err.message}", "err")
                    notify(st, f"Oracle : erreur d'authentification ({err.code}). Arret.")
                    return 4, None
                if status == BADCONF:
                    log(f"{label} : config refusee ({err.code}) - {err.message}", "err")
                    break   # inutile d'insister sur les autres FD avec cette config
                log(f"{label} : erreur {err.status} {err.code} - {err.message}", "err")
                notify(st, f"Oracle : erreur {err.code} - {err.message}")
                return 5, None

    return None, None


def cmd_run(once: bool = False, dry_run: bool = False) -> int:
    cfg = build_oci_config()
    st = Settings.from_env()
    g = Grabber(cfg, st)

    existing = g.existing_instance()
    if existing:
        log(f"'{st.display_name}' existe deja ({existing.lifecycle_state}) - rien a faire", "ok")
        return 0

    ads = g.availability_domains()
    targets = g.usable_targets()
    if not targets:
        log("plus aucun quota Always Free disponible pour ces shapes", "err")
        return 3

    log(f"region {cfg['region']} | {len(ads)} AD | sonde de capacite : "
        f"{'activee' if g.probe_supported else 'desactivee'}")
    log("echelle tentee : " + "  ->  ".join(str(t) for t in targets))

    deadline = (_T0 + st.max_runtime) if st.max_runtime else None
    cycles = 0

    while True:
        cycles += 1
        cycle_start = time.monotonic()

        code, inst = cycle(g, ads, targets, dry_run)
        if code is not None:
            if code == 0 and inst is not None:
                log(f"INSTANCE CREEE : {inst.id}", "ok")
                log(f"shape {inst.shape} | {inst.availability_domain} | {inst.lifecycle_state}", "ok")
                notify(st, f"Oracle Cloud : machine obtenue !\n{inst.shape}\n"
                           f"{inst.availability_domain}\n{inst.id}")
                print(json.dumps({
                    "id": inst.id, "shape": inst.shape,
                    "availability_domain": inst.availability_domain,
                    "state": inst.lifecycle_state, "attempts": g.attempts,
                }, indent=2))
            return code

        if st.max_attempts and g.attempts >= st.max_attempts:
            log(f"MAX_ATTEMPTS={st.max_attempts} atteint", "warn")
            return 2
        if deadline and time.monotonic() >= deadline:
            log(f"duree max atteinte apres {cycles} cycles / {g.attempts} tentatives", "warn")
            return 1
        if once and not g.probe_supported:
            # mode aveugle + --once : un seul passage, le cron relancera
            log("--once : pas de capacite cette fois", "warn")
            return 1

        # Sonde = appel bon marche, on peut boucler vite. Mode aveugle = on espace.
        base = st.probe_interval if g.probe_supported else st.launch_interval
        wait = base + random.randint(0, st.jitter)
        next_deadline = cycle_start + wait
        if deadline and next_deadline >= deadline:
            log(f"duree max atteinte apres {cycles} cycles / {g.attempts} tentatives", "warn")
            return 1
        sleep_until(next_deadline, f"cycle {cycles} termine")


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = args[0] if args and not args[0].startswith("-") else "run"
    if cmd == "discover":
        return cmd_discover()
    if cmd == "probe":
        return cmd_probe()
    if cmd == "run":
        return cmd_run(once="--once" in args, dry_run="--dry-run" in args)
    print(__doc__)
    return 64


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrompu", "warn")
        sys.exit(130)
