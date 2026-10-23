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
| `test_listing.py` | Que un listado sea una sola petición al demonio y no una por contenedor, que lo que solo dice el `inspect` se siga pidiendo, y que un cliente `ssh://` conserve su conexión entre peticiones en vez de abrir un `ssh` nuevo en cada una. Con una comparativa de tiempos impresa |
| `test_real_docker.py` | **Contra Docker de verdad, solo con `--docker`.** Actualiza contenedores con toda la configuración posible y compara el `docker inspect` de antes y después; la ida y vuelta de `/compose`; dependientes de Compose y contenedores que comparten red; tags de un registro privado |

## Contra Docker de verdad

```bash
python3 tests/run_all.py --docker
```

Los dobles dicen si la vuelta atrás borra el contenedor correcto, pero no si el
contenedor que vuelve es el que se fue. Eso solo lo sabe Docker. Estos tests
necesitan un demonio en `/var/run/docker.sock`, levantan un registro en
`127.0.0.1:55000` para publicar versiones «nuevas» de una imagen, y tardan unos
minutos. Solo tocan lo que crean: todo se llama `dcbtest-…` y lleva la etiqueta
`dcbtest=1`, y la limpieza exige las dos cosas.

**Al subir la versión de `docker` en `requirements.txt`, pásalos antes de
publicar.** `docker_update.py` usa dos piezas internas de docker-py
(`_create_container_args` y `api._post_json`) para lo que su API pública no
deja pasar: el `stop_grace_period`, los puertos solo expuestos, las GPU, las
anotaciones y la prioridad de pasarela. Una versión nueva puede cambiarlas sin
avisar, y solo estos tests lo verían.

Se ejecutan en la misma máquina que el demonio, no dentro de un contenedor:
comparten con él `/tmp` (compose monta ficheros que los tests escriben ahí) y
`localhost` (el registro), y el bot no tiene que reconocerse en ningún
contenedor. Necesitan el cliente `docker` con su plugin de Compose. El test
de otra plataforma necesita QEMU registrado, que Docker Desktop ya trae.

## En GitHub

`.github/workflows/ci.yml` pasa las dos baterías en cada push a `main`, en
cada tag y en cada pull request:

- `unit`: Python 3.14 con solo `requirements.txt`.
- `unit-alpine`: lo mismo sobre `alpine:3.24.3` con paramiko y ssh de apk, que
  es lo que corre en producción.
- `docker`: `--docker` en `ubuntu-26.04` (Docker 29 y Compose 5) y en
  `ubuntu-24.04` (Docker 28 y Compose 2). Docker no se porta igual en todas
  sus versiones: con la MAC de una segunda red, o con la que copia en
  `Config.MacAddress`, solo el 28 enseñaba lo que fallaba.

Fuera de GitHub también pasan en Docker 25, 26 y 27, que no tienen runner
propio. Lo que esas versiones no conocen —la prioridad de pasarela, los
montajes de imagen— se salta, y Compose 2.29, que crea la red principal por
su id, no puede devolver la MAC en un segundo `/compose`. Merece la pena
pasarlos ahí al tocar la red de `docker_update.py`: la MAC que Docker saca de
la IP, por ejemplo, solo existe hasta el 27.

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
