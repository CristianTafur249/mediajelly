"""Panel web de MediaJelly: esqueleto de la app, login y plantillas."""
import asyncio
import hmac
import html
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import mediajelly_db
from panel.servicios import acciones as servicio_acciones
from panel.servicios import bd as servicio_bd
from panel.servicios import cola as servicio_cola
from panel.servicios import detalle as servicio_detalle
from panel.servicios import estado as servicio_estado
from panel.servicios import registros as servicio_registros

BASE = Path(__file__).resolve().parent
registro = logging.getLogger(__name__)
MENSAJE_SIN_PASSWORD = "Configura PANEL_PASSWORD en .env para usar el panel."

plantillas = Jinja2Templates(directory=str(BASE / "plantillas"))
plantillas.env.filters.update({
    "nombre_etapa": servicio_estado.nombre_etapa,
    "nombre_fase": servicio_estado.nombre_fase,
    "tiempo": servicio_estado.formato_tiempo,
    "minutos": servicio_estado.minutos_restantes,
    "tamano": servicio_estado.formato_tamano,
})


def _html_estado() -> str:
    datos = servicio_estado.obtener_estado()
    tareas = servicio_cola.listar_tareas_recientes(5)
    return plantillas.get_template("_estado.html").render(estado=datos, tareas=tareas)


async def generar_eventos(intervalo_s: float = 2.0, limite: int | None = None):
    """Eventos SSE con el fragmento de estado; `limite` solo se usa en las pruebas."""
    enviados = 0
    while limite is None or enviados < limite:
        # la BD puede tardar si un ciclo la tiene bloqueada: fuera del bucle de eventos
        html = await asyncio.to_thread(_html_estado)
        una_linea = " ".join(html.replace("\r", "").split("\n"))
        yield f"event: estado\ndata: {una_linea}\n\n"
        enviados += 1
        if limite is None or enviados < limite:
            await asyncio.sleep(intervalo_s)


def _aviso(texto: str, estado: int, destino: str) -> HTMLResponse:
    """Aviso de error para htmx: va a `destino` aunque sea 4xx (ver base.html)."""
    return HTMLResponse(
        f'<p class="aviso">{html.escape(texto)}</p>', status_code=estado,
        headers={"HX-Retarget": destino, "HX-Reswap": "innerHTML"})


def requiere_sesion(request: Request) -> None:
    """Dependencia para las rutas protegidas: sin sesión redirige a /login."""
    if not request.session.get("autenticado"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def _migrar_bd() -> None:
    """Lleva la BD al esquema actual (p. ej. la columna prioridad de la v3).

    Las páginas leen en solo lectura y no migran: sin esto, una BD que dejó una
    versión anterior hace fallar el detalle y la Cola hasta que corra el cron.
    Si falla, se registra y el panel arranca igual.
    """
    try:
        mediajelly_db.init_db()
    except Exception:
        registro.exception("No se pudo preparar la base de datos al arrancar el panel")


@asynccontextmanager
async def _arranque(_app: FastAPI):
    await asyncio.to_thread(_migrar_bd)
    yield


def crear_app(password: str | None = None, secret: str | None = None) -> FastAPI:
    password = password if password is not None else os.environ.get("PANEL_PASSWORD")
    secret = secret or os.environ.get("PANEL_SECRET") or secrets.token_hex(32)

    # init_db va en el arranque y no aquí: `app = crear_app()` corre al importar
    # el módulo (también en los tests, antes de redirigir la BD).
    app = FastAPI(title="Panel MediaJelly", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=_arranque)

    @app.middleware("http")
    async def exigir_password_configurada(request: Request, call_next):
        if not password:
            return PlainTextResponse(MENSAJE_SIN_PASSWORD, status_code=503)
        return await call_next(request)

    # Se agrega después para que quede por fuera y la sesión exista en el resto.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="mediajelly_panel",
        same_site="strict",
        https_only=False,  # en la red local se usa http
        max_age=60 * 60 * 24 * 14,
    )
    app.mount("/estaticos", StaticFiles(directory=str(BASE / "estaticos")), name="estaticos")

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        return plantillas.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, password_enviada: str = Form("", alias="password")):
        if hmac.compare_digest(password_enviada.encode(), password.encode()):
            request.session["autenticado"] = True
            return RedirectResponse("/", status_code=303)
        return plantillas.TemplateResponse(
            request, "login.html", {"error": "Contraseña incorrecta"}
        )

    @app.post("/salir")
    async def salir(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion)])
    async def inicio(request: Request):
        datos = await asyncio.to_thread(servicio_estado.obtener_estado)
        tareas = await asyncio.to_thread(servicio_cola.listar_tareas_recientes, 5)
        return plantillas.TemplateResponse(
            request, "inicio.html",
            {"titulo": "Inicio", "activo": "inicio", "estado": datos, "tareas": tareas}
        )

    @app.post("/ciclo", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion)])
    async def ciclo(request: Request):
        _, creada = await asyncio.to_thread(servicio_acciones.lanzar_ciclo)
        mensaje = ("Ciclo en cola (empieza en menos de 1 min)" if creada
                   else "Ya hay un ciclo en cola")
        return plantillas.TemplateResponse(request, "_mensaje.html", {"mensaje": mensaje})

    @app.get("/eventos", dependencies=[Depends(requiere_sesion)])
    async def eventos():
        return StreamingResponse(
            generar_eventos(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/mas", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion)])
    async def mas(request: Request):
        return plantillas.TemplateResponse(
            request, "mas.html", {"titulo": "Más", "activo": "mas"}
        )

    @app.get("/cola", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion)])
    async def cola(request: Request):
        datos = await asyncio.to_thread(servicio_cola.listar_cola)
        return plantillas.TemplateResponse(
            request, "cola.html", {"titulo": "Cola", "activo": "cola", "cola": datos}
        )

    @app.post("/acciones/{accion}", response_class=HTMLResponse,
              dependencies=[Depends(requiere_sesion)])
    async def accion_cola(request: Request, accion: str, fp: list[str] = Form(default=[])):
        funcion = servicio_acciones.ACCIONES.get(accion)
        if funcion is None:
            raise HTTPException(status_code=404, detail="Acción desconocida")
        if not fp:
            return _aviso("Elige al menos un archivo", 400, "#aviso-cola")
        resultado = await asyncio.to_thread(funcion, fp)
        if resultado.no_encontrados:
            return _aviso("El archivo cambió; recarga la página", 409, "#aviso-cola")
        datos = await asyncio.to_thread(servicio_cola.listar_cola)
        aviso = (f"{len(resultado.en_proceso)} archivo(s) se están procesando; no se cambiaron."
                 if resultado.en_proceso else "")
        return plantillas.TemplateResponse(request, "_cola.html", {"cola": datos, "aviso": aviso})

    @app.get("/video/{fp}", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion)])
    async def video(request: Request, fp: str):
        contexto = {"titulo": "Video", "activo": "cola", "detalle": None,
                    "idiomas": servicio_detalle.IDIOMAS_PERMITIDOS, "error_bd": None}
        try:
            detalle = await asyncio.to_thread(servicio_detalle.obtener_detalle, fp)
        except Exception as exc:
            registro.warning("Lectura del detalle fallida: %s", exc)
            contexto.update(titulo="Video", error_bd=servicio_bd.MENSAJE_BD)
            return plantillas.TemplateResponse(request, "video.html", contexto, status_code=503)
        if detalle is None:
            # tras un retag la huella cambia: el enlace viejo lleva al archivo nuevo
            try:
                nueva = await asyncio.to_thread(servicio_detalle.huella_derivada, fp)
            except Exception as exc:
                registro.warning("Búsqueda de la huella nueva fallida: %s", exc)
                nueva = None
            if nueva:
                return RedirectResponse(f"/video/{quote(nueva, safe=':')}", status_code=303)
            contexto["titulo"] = "Video no encontrado"
            return plantillas.TemplateResponse(request, "video.html", contexto, status_code=404)
        contexto.update(titulo=detalle["archivo"]["nombre"], detalle=detalle)
        return plantillas.TemplateResponse(request, "video.html", contexto)

    @app.post("/video/{fp}/reprocesar", response_class=HTMLResponse,
              dependencies=[Depends(requiere_sesion)])
    async def video_reprocesar(request: Request, fp: str):
        resultado = await asyncio.to_thread(servicio_acciones.reprocesar, [fp])
        if resultado.no_encontrados:
            return _aviso("El archivo cambió; recarga la página", 409, "#aviso-video")
        if resultado.en_proceso:  # el cron lo está comprimiendo: advertencia, no éxito
            contexto = {"mensaje": "El archivo se está procesando; no se cambió.", "clase": "aviso"}
        else:
            contexto = {"mensaje": "Vuelve a pendientes: se procesará en el próximo ciclo.", "clase": "ok"}
        return plantillas.TemplateResponse(request, "_mensaje.html", contexto)

    @app.post("/video/{fp}/detectar", response_class=HTMLResponse,
              dependencies=[Depends(requiere_sesion)])
    async def video_detectar(request: Request, fp: str):
        creada = await asyncio.to_thread(servicio_acciones.detectar_idioma, fp)
        if creada is None:
            return _aviso("El archivo cambió; recarga la página", 409, "#aviso-video")
        mensaje = ("Detección de idioma en cola (empieza en menos de 1 min)" if creada
                   else "Ya hay una detección de idioma en cola")
        return plantillas.TemplateResponse(
            request, "_mensaje.html", {"mensaje": mensaje, "clase": "ok"})

    @app.post("/video/{fp}/idioma", response_class=HTMLResponse,
              dependencies=[Depends(requiere_sesion)])
    async def video_idioma(request: Request, fp: str, pista: str = Form(""), idioma: str = Form("")):
        if idioma not in servicio_detalle.IDIOMAS_PERMITIDOS:
            return _aviso("Idioma no permitido", 400, "#aviso-video")
        # Solo dígitos ASCII y como mucho 3: "²" pasa isdigit() pero int() lo rechaza,
        # y un número de miles de dígitos hace fallar int().
        if not (pista.isascii() and pista.isdecimal() and len(pista) <= 3):
            return _aviso("Pista no válida", 400, "#aviso-video")
        creada = await asyncio.to_thread(servicio_acciones.corregir_idioma, fp, int(pista), idioma)
        if creada is None:
            return _aviso("El archivo cambió; recarga la página", 409, "#aviso-video")
        mensaje = (f"Cambio a {idioma} de la pista {int(pista)} en cola (empieza en menos de 1 min)"
                   if creada else f"Ya hay un cambio a {idioma} de la pista {int(pista)} en cola")
        return plantillas.TemplateResponse(
            request, "_mensaje.html", {"mensaje": mensaje, "clase": "ok"})

    @app.get("/registros", response_class=HTMLResponse, dependencies=[Depends(requiere_sesion)])
    async def registros(request: Request, archivo: str = "ciclo", filtro: str = ""):
        if archivo not in servicio_registros.REGISTROS:
            raise HTTPException(status_code=404, detail="Registro desconocido")
        lineas = await asyncio.to_thread(
            servicio_registros.leer_registro, archivo, 300, filtro.strip())
        contexto = {"titulo": "Registros", "activo": "registros", "lineas": lineas,
                    "archivo": archivo, "filtro": filtro, "registros": servicio_registros.REGISTROS,
                    "etiquetas": servicio_registros.ETIQUETAS}
        if request.headers.get("HX-Request"):
            return plantillas.TemplateResponse(request, "_registro_lineas.html", contexto)
        return plantillas.TemplateResponse(request, "registros.html", contexto)

    return app


app = crear_app()
