"""Formularios de aterrizaje y formularios genéricos de redirección."""

from __future__ import annotations

from plugins.base import (
    Action,
    PageContext,
    Plugin,
    form_action,
    form_fields,
    is_redirect_form,
)


def detect_required(ctx: PageContext) -> Action | None:
    """Formularios que hay que enviar para continuar (landing, send.now...)."""
    soup = ctx.soup

    landing = soup.find("form", id="landing")
    if landing is None:
        go_input = soup.find("input", {"name": "go"})
        if go_input is not None:
            landing = go_input.find_parent("form")
    if landing is not None:
        fields = form_fields(landing)
        if "go" in fields:
            return form_action(landing, fields, ctx.base_url)

    # send.now / 1fichier y similares usan op=download2.
    op_input = soup.find("input", {"name": "op"})
    if op_input is not None and "download" in (op_input.get("value") or "").lower():
        form = op_input.find_parent("form")
        if form is not None:
            return form_action(form, form_fields(form), ctx.base_url)

    for name in ("download", "download2", "dl"):
        button = soup.find("input", {"name": name})
        if button is not None and "download" in (button.get("value") or "").lower():
            form = button.find_parent("form")
            if form is not None:
                return form_action(form, form_fields(form), ctx.base_url)
    return None


def detect_generic(ctx: PageContext) -> Action | None:
    """Si hay un único formulario con campos ocultos, se envía."""
    forms = ctx.soup.find_all("form")
    if len(forms) != 1:
        return None
    form = forms[0]
    fields = form_fields(form)
    if not is_redirect_form(form, fields):
        return None
    return form_action(form, fields, ctx.base_url)


PLUGINS = (
    Plugin(
        name="forms-required",
        priority=40,
        detect=detect_required,
    ),
    Plugin(
        name="forms-generic",
        priority=85,
        detect=detect_generic,
    ),
)
