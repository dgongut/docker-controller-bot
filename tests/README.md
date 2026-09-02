# Tests

```bash
python3 tests/run_all.py
```

Sin dependencias extra: usan solo lo que ya trae el bot. Para filtrar por fichero:

```bash
python3 tests/run_all.py store
```

## Qué cubren

| Fichero | Qué comprueba |
|---|---|
| `test_store.py` | Ajustes, estado y caché de actualizaciones. Ficheros corruptos, claves desconocidas que sobreviven, invalidación por cambio de tag y el volumen antiguo de 4.x |
| `test_migration.py` | Que actualizar desde 4.x no cambie nada visible: valores importados tal cual, el entorno sin ganar tras el sembrado, y el silencio activo trasladado |
| `test_bot.py` | Ajustes leídos en caliente, los menús de `/settings` y `/start`, y el registro de callbacks |

## Cómo funcionan

El bot es un único módulo que hace todo su arranque al importarse. `harness.py`
lo importa con Docker sustituido por un doble y el almacenamiento en un
directorio temporal, que basta para ejercitar casi todo sin demonio ni token.

`test_bot.py` comparte una sola instancia del módulo entre todos sus tests, a
propósito: el módulo es estado global por diseño. Por eso **ningún test puede
asertar sobre un valor de arranque leyéndolo en vivo** — se capturan en `SEEDED`
al importar. Si añades uno que dependa del orden, se romperá en cuanto alguien
inserte otro antes por orden alfabético.

## La línea base congelada

`data/callbacks_4.2.0.json` es una foto de los 73 callbacks de la v4.2.0 y de
los metadatos que el despachador usaba para cada uno, sacados de
`CALL_PATTERNS`, `PROJECT_COMMANDS`, `MULTI_ACTION_COMMANDS` y de la lista de
exclusión de borrado de mensajes que vivía dentro de `button_controller`.

`test_the_registry_matches_4_2_0_exactly` compara el registro contra esa foto,
así que un refactor no puede cambiar en silencio cómo se comporta un botón. **No
se regenera**: si un test falla contra ella, o el cambio es intencionado y se
actualiza a mano explicando por qué, o es un bug.
