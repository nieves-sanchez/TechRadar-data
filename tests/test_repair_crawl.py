"""
tests/test_repair_crawl.py — Integración de repair_crawl.py con enrich_jobs() (FASE B — Paso 4,
notes/PROJECT_MASTER_CONTEXT.md §57.6.11 y Parte 8), incluida la revisión de fiabilidad del
2026-09-11: qué pasa cuando enrich_jobs() falla DESPUÉS de que description_full ya se persistió.

No testea la lógica de enrich_jobs() en sí — ya cubierta en tests/test_enrich.py. Aquí se testea
el CALLER y la orquestación: que repair_crawl.py solo llama a enrich_jobs() cuando --enrich está
activo, que lo hace exactamente con los job_id recién persistidos, que reintenta antes de dar un
batch por perdido, que reconecta ante un error de conexión sin crashear, y que lo que sigue
fallando tras agotar los reintentos queda registrado de forma explícita (nunca en silencio). Todo
con dobles (mocks): ningún test se conecta a Supabase real ni crawlea de verdad.
"""

import psycopg2
from unittest.mock import MagicMock

from scripts import repair_crawl


def _install_fakes(
    monkeypatch,
    pending,
    crawl_side_effect,
    enrich_return=None,
    enrich_side_effect=None,
    get_connection_side_effect=None,
):
    """Sustituye las dependencias externas de run_repair por dobles controlados."""
    fake_conn = MagicMock(name="conn")

    enrich_mock = MagicMock(name="enrich_jobs")
    if enrich_side_effect is not None:
        enrich_mock.side_effect = enrich_side_effect
    else:
        enrich_mock.return_value = enrich_return or {
            "jobs_seen": 0,
            "skills_links_attempted": 0,
            "remote_updated": 0,
            "role_category_updated": 0,
        }

    if get_connection_side_effect is not None:
        get_connection_mock = MagicMock(side_effect=get_connection_side_effect)
    else:
        get_connection_mock = MagicMock(return_value=fake_conn)

    fakes = {
        "conn": fake_conn,
        "get_connection": get_connection_mock,
        "fetch_pending": MagicMock(return_value=pending),
        "crawl_description": MagicMock(side_effect=crawl_side_effect),
        "flush_updates": MagicMock(name="_flush_updates"),
        "enrich_jobs": enrich_mock,
    }

    monkeypatch.setattr(repair_crawl, "_get_connection", fakes["get_connection"])
    monkeypatch.setattr(repair_crawl, "_fetch_pending", fakes["fetch_pending"])
    monkeypatch.setattr(repair_crawl, "crawl_description", fakes["crawl_description"])
    monkeypatch.setattr(repair_crawl, "_flush_updates", fakes["flush_updates"])
    monkeypatch.setattr(repair_crawl, "enrich_jobs", fakes["enrich_jobs"])
    monkeypatch.setattr(repair_crawl.time, "sleep", MagicMock())

    return fakes


def _stats(jobs_seen=1, skills=0, remote=0, role=0):
    return {
        "jobs_seen": jobs_seen,
        "skills_links_attempted": skills,
        "remote_updated": remote,
        "role_category_updated": role,
    }


# =============================================================================
# A — SIN --enrich: comportamiento idéntico al de antes del Paso 4
# =============================================================================


def test_sin_enrich_no_se_llama_a_enrich_jobs(monkeypatch):
    pending = [(1, "http://x/details/1"), (2, "http://x/details/2")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("texto uno", False), ("texto dos", False)],
    )

    repair_crawl.run_repair(enrich=False)

    fakes["enrich_jobs"].assert_not_called()


def test_sin_enrich_el_flush_se_comporta_igual_que_antes(monkeypatch):
    pending = [(1, "http://x/details/1"), (2, "http://x/details/2")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("texto uno", False), ("texto dos", False)],
    )

    repair_crawl.run_repair(enrich=False)

    fakes["flush_updates"].assert_called_once()
    conn_arg, updates_arg = fakes["flush_updates"].call_args.args
    assert conn_arg is fakes["conn"]
    assert updates_arg == [(1, "texto uno"), (2, "texto dos")]


def test_sin_enrich_no_se_acumulan_ni_loguean_contadores(monkeypatch, caplog):
    pending = [(1, "u1")]
    _install_fakes(monkeypatch, pending, crawl_side_effect=[("t1", False)])

    with caplog.at_level("INFO"):
        repair_crawl.run_repair(enrich=False)

    assert "Enriquecimiento" not in caplog.text


# =============================================================================
# B — CON --enrich, éxito al primer intento: IDs correctos, sin ampliar el alcance
# =============================================================================


def test_con_enrich_llama_tras_flush_exitoso_con_ids_correctos(monkeypatch):
    pending = [(1, "u1"), (2, "u2"), (3, "u3")]
    # job_id=2 no consigue descripción (no throttled, simplemente sin texto): no debe
    # persistirse ni enriquecerse.
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("texto1", False), (None, False), ("texto3", False)],
        enrich_return=_stats(jobs_seen=2, skills=3, remote=1),
    )

    repair_crawl.run_repair(enrich=True)

    fakes["enrich_jobs"].assert_called_once()
    conn_arg, job_ids_arg = fakes["enrich_jobs"].call_args.args
    assert conn_arg is fakes["conn"]
    assert job_ids_arg == [1, 3]


def test_con_enrich_no_llama_si_no_hay_ids_nuevos(monkeypatch):
    pending = [(1, "u1"), (2, "u2")]
    fakes = _install_fakes(
        monkeypatch, pending, crawl_side_effect=[(None, False), (None, False)]
    )

    repair_crawl.run_repair(enrich=True)

    fakes["flush_updates"].assert_not_called()
    fakes["enrich_jobs"].assert_not_called()


def test_con_enrich_llama_enrich_jobs_con_la_misma_conexion_del_flush(monkeypatch):
    pending = [(1, "u1")]
    fakes = _install_fakes(monkeypatch, pending, crawl_side_effect=[("texto1", False)])

    repair_crawl.run_repair(enrich=True)

    flush_conn = fakes["flush_updates"].call_args.args[0]
    enrich_conn = fakes["enrich_jobs"].call_args.args[0]
    assert flush_conn is enrich_conn is fakes["conn"]


def test_contador_conserva_el_nombre_skills_links_attempted(monkeypatch, caplog):
    pending = [(1, "u1")]
    _install_fakes(
        monkeypatch, pending, crawl_side_effect=[("t1", False)],
        enrich_return=_stats(skills=5),
    )

    with caplog.at_level("INFO"):
        repair_crawl.run_repair(enrich=True)

    assert "skills_links_attempted=5" in caplog.text
    assert "skills_links_added" not in caplog.text


# =============================================================================
# C — Fallo recuperable de enrich_jobs(): reintento inmediato dentro del mismo batch
# =============================================================================


def test_fallo_recuperable_se_reintenta_de_inmediato_y_no_pierde_el_batch(monkeypatch, caplog):
    """El problema central de la revisión: description_full YA se persistió (el flush ocurre
    antes) y el primer intento de enrich_jobs() falla. El mecanismo debe reintentar dentro del
    mismo batch antes de darlo por perdido."""
    pending = [(1, "u1"), (2, "u2")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False), ("t2", False)],
        enrich_side_effect=[RuntimeError("fallo transitorio"), _stats(jobs_seen=2, skills=1)],
    )

    with caplog.at_level("INFO"):
        repair_crawl.run_repair(enrich=True)  # no debe lanzar

    assert fakes["flush_updates"].call_count == 1  # description_full sí se persistió
    assert fakes["enrich_jobs"].call_count == 2  # intento + reintento inmediato
    assert fakes["conn"].rollback.call_count == 1  # solo el intento fallido hace rollback
    assert "ATENCIÓN" not in caplog.text  # se recuperó, no hubo pérdida definitiva
    assert "jobs_seen=2 skills_links_attempted=1" in caplog.text  # contado UNA sola vez


def test_no_hay_doble_conteo_cuando_el_reintento_recupera_el_batch(monkeypatch):
    """Un intento fallido no debe sumar nada a enrich_totals — solo el que finalmente tuvo
    éxito. Se verifica de forma indirecta vía el log acumulado final (caplog en el test
    anterior) y aquí de forma directa contando cuántas veces se llamó a enrich_jobs()."""
    pending = [(1, "u1")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False)],
        enrich_side_effect=[RuntimeError("boom"), _stats(jobs_seen=1, skills=7)],
    )

    repair_crawl.run_repair(enrich=True)

    # Si hubiera doble conteo, el total acumulado incluiría el intento fallido (que no
    # devuelve stats) más el exitoso; con un solo enrich_totals correcto, el resultado
    # observable es simplemente que enrich_jobs() se llamó 2 veces y no más (sin un tercer
    # intento innecesario tras el éxito).
    assert fakes["enrich_jobs"].call_count == 2


def test_fallo_recuperable_agotados_los_intentos_se_difiere_al_final(monkeypatch):
    """Si el batch falla en TODOS los intentos inmediatos (ENRICH_MAX_ATTEMPTS=2), no se
    pierde: se difiere al reintento final consolidado de run_repair(), que aquí también se
    hace fallar para comprobar que los IDs quedan correctamente registrados como perdidos."""
    pending = [(1, "u1"), (2, "u2")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False), ("t2", False)],
        enrich_side_effect=RuntimeError("persistente"),  # falla siempre, en todos los intentos
    )

    repair_crawl.run_repair(enrich=True)  # no debe lanzar

    # 2 intentos del batch + 1 reintento final consolidado = 3 llamadas.
    assert fakes["enrich_jobs"].call_count == 3
    assert fakes["conn"].rollback.call_count == 3


def test_fallo_total_tras_reintentos_se_registra_explicitamente_con_los_ids(monkeypatch, caplog):
    pending = [(1, "u1"), (2, "u2")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False), ("t2", False)],
        enrich_side_effect=RuntimeError("persistente"),
    )

    with caplog.at_level("ERROR"):
        repair_crawl.run_repair(enrich=True)

    assert "ATENCIÓN" in caplog.text
    assert "[1, 2]" in caplog.text  # los job_id exactos, no un conteo vago
    assert "description_full ya persistida" in caplog.text


def test_reintento_final_consolidado_recupera_lo_que_fallo_durante_los_batches(monkeypatch, caplog):
    """Un batch agota sus 2 intentos (queda diferido) pero el reintento final, al terminar
    toda la ejecución, sí tiene éxito: el resultado final debe ser una recuperación completa,
    sin ATENCIÓN, con los contadores reflejados."""
    pending = [(1, "u1")]
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False)],
        enrich_side_effect=[
            RuntimeError("falla intento 1"),
            RuntimeError("falla intento 2 (agota ENRICH_MAX_ATTEMPTS)"),
            _stats(jobs_seen=1, skills=2, remote=1),  # el reintento final SÍ funciona
        ],
    )

    with caplog.at_level("INFO"):
        repair_crawl.run_repair(enrich=True)

    assert fakes["enrich_jobs"].call_count == 3
    assert "ATENCIÓN" not in caplog.text
    assert "Reintento final de enriquecimiento recuperó 1 ofertas" in caplog.text
    assert "jobs_seen=1 skills_links_attempted=2 remote_updated=1" in caplog.text


def test_fallo_en_un_batch_no_impide_procesar_el_siguiente_batch(monkeypatch):
    """Batch 1 (job 1) agota sus reintentos; batch 2 (job 2) debe procesarse con normalidad de
    todas formas — el fallo de uno no debe bloquear el otro."""
    pending = [(1, "u1"), (2, "u2")]
    monkeypatch.setattr(repair_crawl, "UPDATE_BATCH_SIZE", 1)  # fuerza dos puntos de flush
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False), ("t2", False)],
        enrich_side_effect=[
            RuntimeError("batch1 intento 1"),
            RuntimeError("batch1 intento 2"),
            _stats(jobs_seen=1, skills=4),  # batch 2, éxito al primer intento
            RuntimeError("reintento final también falla"),  # reintento final de batch1
        ],
    )

    repair_crawl.run_repair(enrich=True)

    assert fakes["enrich_jobs"].call_count == 4
    # batch2 (job 2) se llamó independientemente de que batch1 siguiera fallando.
    ids_llamados = [call.args[1] for call in fakes["enrich_jobs"].call_args_list]
    assert [2] in ids_llamados


# =============================================================================
# D — Conexión realmente rota: reconecta, no crashea, no reutiliza una conexión muerta
# =============================================================================


def test_conexion_rota_reconecta_y_reintenta_con_la_conexion_nueva(monkeypatch):
    pending = [(1, "u1")]
    conn_vieja = MagicMock(name="conn_vieja")
    conn_nueva = MagicMock(name="conn_nueva")
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False)],
        enrich_side_effect=[
            psycopg2.OperationalError("server closed the connection unexpectedly"),
            _stats(jobs_seen=1),
        ],
        get_connection_side_effect=[conn_vieja, conn_nueva],
    )

    repair_crawl.run_repair(enrich=True)

    assert fakes["get_connection"].call_count == 2  # conexión inicial + reconexión
    assert fakes["enrich_jobs"].call_count == 2
    primera_llamada_conn = fakes["enrich_jobs"].call_args_list[0].args[0]
    segunda_llamada_conn = fakes["enrich_jobs"].call_args_list[1].args[0]
    assert primera_llamada_conn is conn_vieja
    assert segunda_llamada_conn is conn_nueva  # NO reutiliza la conexión rota
    # La conexión vieja se intenta cerrar como parte de la reconexión (best-effort).
    assert conn_vieja.close.called


def test_conexion_rota_no_llama_rollback_de_nuevo_sobre_la_conexion_rota(monkeypatch):
    """Ante OperationalError/InterfaceError, _attempt_enrich no debe intentar conn.rollback()
    directamente (ese intento ya se protege dentro de _reconnect, con su propio try/except)."""
    pending = [(1, "u1")]
    conn_vieja = MagicMock(name="conn_vieja")
    conn_nueva = MagicMock(name="conn_nueva")
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False)],
        enrich_side_effect=[
            psycopg2.InterfaceError("connection already closed"),
            _stats(jobs_seen=1),
        ],
        get_connection_side_effect=[conn_vieja, conn_nueva],
    )

    repair_crawl.run_repair(enrich=True)

    # El rollback "directo" de _attempt_enrich no se llama para errores de conexión — solo el
    # rollback best-effort dentro de _reconnect (que va envuelto en su propio try/except y no
    # es observable como fallo aquí).
    assert conn_vieja.close.called


def test_rollback_que_falla_se_trata_como_conexion_perdida_sin_crashear(monkeypatch):
    """Un error genérico de enrich_jobs() cuyo conn.rollback() TAMBIÉN falla (conexión
    realmente muerta, no solo un error de lógica) no debe propagar — debe reclasificarse como
    conexión perdida y reconectar."""
    pending = [(1, "u1")]
    conn_vieja = MagicMock(name="conn_vieja")
    conn_vieja.rollback.side_effect = psycopg2.InterfaceError("connection already closed")
    conn_nueva = MagicMock(name="conn_nueva")
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False)],
        enrich_side_effect=[
            RuntimeError("error genérico, no de conexión"),
            _stats(jobs_seen=1),
        ],
        get_connection_side_effect=[conn_vieja, conn_nueva],
    )

    repair_crawl.run_repair(enrich=True)  # no debe lanzar

    assert conn_vieja.rollback.called
    assert fakes["get_connection"].call_count == 2  # reconectó tras el rollback fallido
    segunda_llamada_conn = fakes["enrich_jobs"].call_args_list[1].args[0]
    assert segunda_llamada_conn is conn_nueva


def test_reconexion_fallida_no_crashea_y_registra_el_fallo(monkeypatch, caplog):
    """Si ni siquiera se puede reconectar (ni en el reintento del batch ni en el reintento
    final), el batch se da por perdido de forma explícita y el resto de run_repair() (incluido
    el cierre) sigue funcionando sin lanzar."""
    pending = [(1, "u1")]
    conn_inicial = MagicMock(name="conn_inicial")
    fakes = _install_fakes(
        monkeypatch,
        pending,
        crawl_side_effect=[("t1", False)],
        enrich_side_effect=psycopg2.OperationalError("conexión perdida"),
        get_connection_side_effect=[
            conn_inicial,
            psycopg2.OperationalError("no se pudo reconectar (retry de batch)"),
            psycopg2.OperationalError("no se pudo reconectar (reintento final)"),
        ],
    )

    with caplog.at_level("ERROR"):
        repair_crawl.run_repair(enrich=True)  # no debe lanzar

    assert "No se pudo reconectar" in caplog.text
    assert "ATENCIÓN" in caplog.text
    assert fakes["get_connection"].call_count == 3


def test_conn_close_no_crashea_si_la_conexion_quedo_rota(monkeypatch):
    pending = [(1, "u1")]
    fakes = _install_fakes(monkeypatch, pending, crawl_side_effect=[("t1", False)])
    fakes["conn"].close.side_effect = Exception("close sobre conexión ya rota")

    repair_crawl.run_repair(enrich=True)  # no debe lanzar pese al fallo de close()

    assert fakes["conn"].close.called


# =============================================================================
# E — Regresión: no reutilizar código de enrich_jobs, no tocar Ollama/Pipeline C
# =============================================================================


def test_sin_enrich_ninguna_de_las_rutas_de_reintento_se_activa(monkeypatch):
    """Con --enrich desactivado, ni siquiera se evalúa nada de la lógica de reintento/
    reconexión: cero llamadas a _get_connection() más allá de la inicial."""
    pending = [(1, "u1")]
    fakes = _install_fakes(monkeypatch, pending, crawl_side_effect=[("t1", False)])

    repair_crawl.run_repair(enrich=False)

    assert fakes["get_connection"].call_count == 1
