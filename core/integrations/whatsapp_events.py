"""Readable, bounded previews for WhatsApp events that are not text or media.

These are conversation data, never instructions or input to triage. The queue
and the session repository encrypt them like every other customer message.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata


def _text(value, limit: int = 250) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if unicodedata.category(c)[0] != "C").strip()[:limit]


def preview(kind: str, body, errors) -> str:
    if kind == "reaction" and isinstance(body, dict):
        emoji = _text(body.get("emoji"), 32)
        return f"Reação: {emoji}" if emoji else "A pessoa removeu uma reação."
    if kind == "location" and isinstance(body, dict):
        lat, lon = body.get("latitude"), body.get("longitude")
        valid = (
            isinstance(lat, (int, float))
            and not isinstance(lat, bool)
            and isinstance(lon, (int, float))
            and not isinstance(lon, bool)
            and math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        )
        parts = ["Localização compartilhada:"]
        parts += [
            part for part in (_text(body.get("name")), _text(body.get("address"), 500)) if part
        ]
        parts.append(
            f"Latitude: {lat}; longitude: {lon}" if valid else "Coordenadas indisponíveis."
        )
        return "\n".join(parts)
    if kind == "contacts" and isinstance(body, list):
        lines = ["Contatos compartilhados:"]
        for item in body[:5]:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or {}
            name = name if isinstance(name, dict) else {}
            label = (
                _text(name.get("formatted_name"))
                or " ".join(
                    filter(None, (_text(name.get("first_name")), _text(name.get("last_name"))))
                )
                or "Sem nome"
            )
            phones = item.get("phones") or []
            numbers = (
                [_text(p.get("phone"), 60) for p in phones[:3] if isinstance(p, dict)]
                if isinstance(phones, list)
                else []
            )
            emails = item.get("emails") or []
            addresses = (
                [_text(e.get("email"), 160) for e in emails[:2] if isinstance(e, dict)]
                if isinstance(emails, list)
                else []
            )
            lines.append("• " + " · ".join(filter(None, [label, *numbers, *addresses])))
        if len(body) > 5:
            lines.append("Outros contatos não foram incluídos nesta prévia.")
        return "\n".join(lines)[:3900]
    if kind == "system" and isinstance(body, dict):
        description = _text(body.get("body"), 1000)
        if description:
            return "Aviso do WhatsApp: " + description
    safe_kind = kind if re.fullmatch(r"[a-z_]{1,40}", kind) else "desconhecido"
    lines = [f"Evento recebido pelo WhatsApp: {safe_kind}."]
    if isinstance(errors, list):
        codes = [
            str(error["code"])
            for error in errors[:5]
            if isinstance(error, dict)
            and isinstance(error.get("code"), int)
            and not isinstance(error.get("code"), bool)
        ]
        if codes:
            lines.append("Código informado pelo WhatsApp: " + ", ".join(codes) + ".")
    if body:
        # Keep the available customer content, rather than silently replacing
        # it with a claim that a photo was sent. Never store the whole webhook.
        data = json.dumps(body, ensure_ascii=False, default=str)
        lines.append("Conteúdo disponível: " + data[:2500])
        if len(data) > 2500:
            lines.append("Prévia limitada. O restante não foi preservado.")
    else:
        lines.append("O WhatsApp não forneceu conteúdo legível para este evento.")
    return "\n".join(lines)[:3900]
