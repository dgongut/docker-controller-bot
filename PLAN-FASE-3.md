# Fase 3 — el cimiento de rutas

> ## ⏸️ APARCADO el 2026-09-03
>
> `/deploy` y `/backup` quedan fuera de la 5.0.0, y con ellos este cimiento,
> que **no tiene propósito propio**: existía solo para que esas dos escribieran
> en él. El documento se conserva porque el diseño está razonado y los seis
> riesgos de la sección 7 siguen siendo los que hay que verificar si se
> retoma — y el de armv7 es el que decide.

Plan de diseño, escrito el 2026-09-03. Nota de trabajo como `NOTAS-5.0.0.md`:
bórralo o añádelo a `.gitignore` antes de publicar.

Nada de esto está implementado. El objetivo del documento era que la decisión
estuviera tomada y razonada **antes** de escribir la capa sobre la que se
apoyan `/deploy` y `/backup`.

---

## 1. Qué tiene que resolver

`/deploy` y `/backup` necesitan dos cosas que el bot hoy no puede hacer:

1. **Leer y escribir ficheros** en el directorio de stacks de cada host: el
   `docker-compose.yml`, su `.env`, y el contenido de los volúmenes nombrados.
2. **Ejecutar `docker compose up -d`** en ese host, en el directorio del stack.

Y hay una restricción que decide casi todo el diseño:

> **El bot no invoca nunca el CLI de `docker`.** Todo va por el SDK
> (`grep -rn "subprocess\|Popen" *.py` → cero resultados). Y `docker compose`
> es un plugin de CLI: no tiene equivalente en el SDK.

## 2. La decisión: todo por la API de Docker

El bot lanza un **contenedor auxiliar efímero en el host destino**, con el
directorio de stacks y el socket montados. Contra ese contenedor:

- lee ficheros con `container.get_archive(path)` (devuelve un tar);
- escribe ficheros con `container.put_archive(path, tar)`;
- ejecuta compose con `container.exec_run(cmd, workdir=...)`.

Por qué esta vía y no otra:

- **Es idéntica en `unix://`, `tcp://` y `ssh://`.** Una sola ruta de código.
  Cualquier otra opción parte el soporte por transporte, justo después de que
  la fase 2 lo unificara en todo lo demás.
- **No añade ni protocolo ni dependencia.** Ni sftp, ni NFS/SMB, ni `ssh` exec.
- **No pide nada al usuario salvo la ruta de stacks de cada host.** Un montaje
  de red por host remoto es carga de configuración real, y además se cuelga:
  un NFS caído bloquearía al bot en una llamada de fichero, dentro de un worker
  de telebot. Eso es exactamente el problema que acabamos de quitar de
  `/version` y `/donate`.
- **El patrón ya existe en el repo.** `core.py:588` lanza
  `UPDATER_IMAGE` con el socket montado y `detach=True`, y `delete_updater()`
  lo barre al arrancar. El auxiliar de la fase 3 es esa misma forma,
  generalizada a cualquier host.

### Lo que ya está confirmado

| | |
|---|---|
| `Container.get_archive(path)` | existe en docker-py 7.x |
| `Container.put_archive(path, data)` | existe |
| `Container.exec_run(cmd, workdir=...)` | `workdir` es parámetro real |
| `containers.run(image, **kwargs)` | pasa `volumes`, `detach`, `environment`, `entrypoint` a `create` |

## 3. El esquema en settings

La fase 1 ya reservó el hueco. En `store.py:57`:

```python
# Host paths later features need to write into (stacks, backups), keyed by
# host id.
"paths": {},
```

Se rellena así:

```json
"paths": {
    "h_cf94": {"stacks": "/opt/stacks"},
    "h_6eb4": {"stacks": "/srv/docker"}
}
```

Claves por **id de host**, no por alias: el id no cambia nunca y el alias sí,
que es la razón por la que las programaciones y las cachés ya se guardan así.

Decisiones que van con el esquema:

- **Fuera de la entrada del host.** `hosts` es la lista que valida `add_host`
  y que reescribe `rename_host` bajo lock; `paths` es un documento aparte para
  que añadir una ruta no toque la lista de conexiones. Un host sin ruta es
  perfectamente válido: `/deploy` no está disponible para él y se dice así.
- **Se pregunta en la pantalla del host**, junto a probar / renombrar / quitar
  (`build_settings_host`, `core.py:2375`). Un botón más y un
  `ask_text_input`, como el de renombrar.
- **`remove_host` tiene que barrer su entrada de `paths`.** Es el mismo agujero
  que las programaciones huérfanas que acabamos de cerrar. Y con el mismo
  criterio: se avisa antes de la pulsación.

## 4. Las dos fuentes de una ruta, y por qué no valen lo mismo

El bot ya lee `com.docker.compose.project.working_dir` y
`...config_files` de las etiquetas de los contenedores
(`docker_compose_manager.py:11`), y los muestra en `/info` de un proyecto. O
sea: **para un stack que ya existe, el bot ya sabe dónde vive.** La ruta
configurada solo hace falta para un stack **nuevo**.

Pero las dos fuentes no merecen la misma confianza:

> Una etiqueta de compose la pone quien arranca el contenedor. Cualquiera que
> pueda lanzar un contenedor en ese host elige la cadena que el bot va a leer.

De ahí la regla:

- Una ruta **descubierta** se puede leer y mostrar. Ya se hace hoy, y va
  escapada porque el mensaje es HTML.
- Una ruta descubierta **no puede ser origen de un bind-mount** del auxiliar
  ni destino de una escritura, salvo que se compruebe que está dentro de la
  raíz de stacks configurada para ese host.
- La comprobación es textual y conservadora —normalizar, rechazar `..`,
  exigir que empiece por `raíz + "/"`—, porque la ruta está en **otra
  máquina** y el bot no puede hacerle `realpath`.

Proporción, para no venderlo como más de lo que es: el bot ya tiene control
total de Docker en cada host configurado, que es equivalente a root. Quien
pueda falsear esa etiqueta ya tiene ese acceso. La regla no impide una
escalada; lo que hace es que el peor caso de una etiqueta mala sea *que no
pase nada* en vez de *que el bot escriba un fichero en un sitio raro*. Es
barata y se gana el sitio.

**El auxiliar monta la raíz, no el subdirectorio descubierto.** Así el kernel
resuelve los enlaces simbólicos dentro de la vista del propio auxiliar. Queda
un riesgo residual: un enlace dentro de la raíz que apunte fuera. Quien pueda
plantarlo ya tiene escritura en el directorio de stacks, así que se documenta
y se deja.

## 5. La invariante de la ruta idéntica, reubicada

En las notas la fase 3 era «directorio de stacks con ruta idéntica dentro y
fuera del contenedor». Con esta vía el bot no toca el sistema de ficheros, así
que la invariante ya no es sobre *su* contenedor. Pero **sobrevive, movida al
auxiliar**, y por una razón concreta:

> El auxiliar monta cada ruta del host **en la misma ruta dentro de sí mismo**.

Porque compose resuelve las rutas relativas del fichero (`./data:/data`)
contra su directorio de trabajo, y luego manda el resultado al daemon como
origen de un bind-mount, que el daemon interpreta **en el host**. Si el
auxiliar viera el stack en `/work` mientras el host lo tiene en
`/opt/stacks/foo`, compose emitiría `/work/data` como origen y el daemon
crearía un directorio vacío en una ruta que no significa nada. Montándolo en
la ruta idéntica los dos marcos son el mismo marco: no hay nada que traducir y
desaparece la clase de bug entera.

De ahí la invariante nueva, en el mismo tono que la regla de oro del
multi-host:

> **Una ruta que llega a la API de Docker es siempre una ruta del host.**
> Nunca una ruta vista desde dentro de un contenedor. Y el auxiliar existe
> precisamente para que las dos coincidan.

## 6. El contenedor auxiliar: mecánica

```python
with host_workspace(host_id, root) as ws:      # levanta el auxiliar
    data = ws.read("/opt/stacks/foo/docker-compose.yml")
    ws.write("/opt/stacks/foo/docker-compose.yml", nuevo)
    salida = ws.run(["docker", "compose", "up", "-d"], cwd="/opt/stacks/foo")
```

Ciclo de vida:

1. `containers.run(HELPER_IMAGE, name=HELPER_CONTAINER_NAME, command=<no-op
   largo>, volumes={root: {bind: root}, socket: {bind: socket}}, detach=True)`.
2. **Arrancado, no solo creado.** `get_archive` sobre un bind-mount de un
   contenedor parado es dudoso —el montaje no está activo—, así que se
   mantiene vivo con un no-op durante la operación. Esto está en la lista de
   riesgos a verificar (§7): si resultara que funciona parado, se ahorra el
   arranque, pero **el plan no depende de ello**.
3. Se para y se quita en un `finally`. Sin `auto_remove`, que compite con la
   lectura del código de salida.
4. **Se barre al arrancar el bot**, en cada host alcanzable, igual que
   `delete_updater()`. Si el bot muere a mitad de un `/deploy`, el auxiliar se
   queda; un nombre reconocible y un barrido lo resuelven.
5. Un auxiliar por host y **uno a la vez**: el nombre es fijo, así que dos
   operaciones simultáneas chocarían. Un lock por host id, con el orden
   `core` → `host_registry` que ya fija
   `test_the_module_that_locks_last_depends_on_nothing_above_it`.

El socket: para el host local es `/var/run/docker.sock`. Para un `tcp://` o
`ssh://` es el socket **de esa máquina**, que normalmente está en la misma
ruta pero no tiene por qué. Va en los riesgos.

Las operaciones de fichero **no necesitan la imagen de compose**: para
`get_archive` / `put_archive` basta un contenedor con el montaje, sin shell
siquiera. Se podría usar cualquier imagen que el host ya tenga y ahorrar la
descarga. No entra en el plan: elegir «cualquier imagen» es frágil, y una
descarga por host es un precio razonable. Queda anotado como optimización.

## 7. Riesgos a verificar en hardware real

Esto **no** son detalles de implementación: si alguno sale mal, el diseño
cambia. Se comprueban en los tres hosts de pruebas antes de escribir código de
verdad.

1. **armv7.** El proyecto soporta armv7, y es la razón por la que paramiko
   entra por `apk` y no por `pip` (`cryptography` no tiene rueda musl para esa
   arquitectura). Hay que confirmar que la imagen auxiliar publica variante
   `linux/arm/v7`. Si no la publica, esos usuarios se quedan sin `/deploy` y
   hay que decidir qué se les dice.
2. **Qué imagen lleva compose v2.** Candidata: `docker:cli`, de Alpine. Hay
   que confirmar que el plugin `compose` viene dentro y **fijar el tag**, no
   `latest`: el resto del proyecto ya fija versiones a propósito.
3. **`get_archive` sobre un bind-mount de un contenedor parado.** Determina si
   hace falta arrancar el auxiliar para leer un fichero. El plan asume que sí
   (§6.2); confirmarlo puede simplificarlo.
4. **La ruta del socket en un host remoto.** Si no siempre es
   `/var/run/docker.sock`, hay que sacarla de `client.info()` o preguntarla.
5. **Un host sin salida a internet.** La descarga de la imagen auxiliar falla.
   Necesita su mensaje, no una traza.
6. **Rootless y Podman.** Si alguien apunta el bot a un daemon rootless, el
   socket está en otro sitio y los permisos de escritura del auxiliar sobre el
   directorio de stacks cambian. Basta con saber si se rompe y decirlo.

## 8. La API del cimiento

Lo que la fase 3 entrega, y nada más:

```python
# --- rutas ---
stacks_root(host_id)               -> str | None    # la configurada
set_stacks_root(host_id, path)     -> bool          # valida y guarda
stack_dir(host_id, project_name)   -> str | None    # descubierta, si no raíz/proyecto
compose_file(host_id, project_name)-> str | None    # de la etiqueta config_files
is_inside_root(host_id, path)      -> bool          # la regla de §4

# --- espacio de trabajo en el host ---
host_workspace(host_id, root)      -> context manager
    .read(path)                    -> bytes
    .write(path, data)             -> None
    .run(argv, cwd=None)           -> (código, salida)
```

Módulo nuevo, `host_paths.py`, con la misma dependencia de un solo sentido que
`host_registry`: **no importa `core`**. Eso lo obliga el lint del orden de
locks, que hay que ampliar con el módulo nuevo.

## 9. Plan de tests

Al estilo del repo: comprobar que el test falla antes del arreglo.

| Test | Fija |
|---|---|
| `test_a_path_from_a_label_is_never_written_to` | La regla de §4, con una etiqueta que apunta fuera de la raíz |
| `test_a_path_with_dot_dot_is_rejected` | Normalización y `..` |
| `test_the_helper_mounts_every_path_at_its_own_path` | La invariante de §5, leyendo los `volumes` con que se lanza |
| `test_the_helper_is_removed_even_when_the_operation_fails` | El `finally` |
| `test_a_leftover_helper_is_swept_at_startup` | El barrido, con un auxiliar preexistente |
| `test_two_deploys_on_one_host_do_not_overlap` | El lock por host |
| `test_removing_a_host_forgets_its_paths` | El barrido de `paths` en `remove_host` |
| `test_a_host_without_a_stacks_path_says_so` | Que `/deploy` no aparezca a medias |
| Lint: `host_paths.py` no importa `core` | Ampliar `LOCK_HOLDERS` |

El SDK va stubbeado como ya lo hace `tests/harness.py`, que sustituye
`docker.DockerClient`. Los `volumes` con que se lanza el auxiliar se leen del
`MagicMock`, que es como se comprueba la invariante de la ruta idéntica sin
Docker de verdad.

## 10. Por dónde entra la fase 4

Con el cimiento puesto:

- **`/deploy`**: recibe un `docker-compose.yml` (fichero o texto), lo escribe
  en `raíz/nombre/docker-compose.yml` con `ws.write`, y lanza
  `ws.run(["docker","compose","up","-d"], cwd=...)`. Confirmación antes, con
  su `host_label`, porque escribe y arranca cosas.
- **`/backup`**: lee el compose y el `.env` con `ws.read`, y para los volúmenes
  nombrados levanta el auxiliar con el volumen montado y saca un tar. Mismo
  patrón, distinto montaje.

Las dos son host-scoped, así que van a `HOST_SCOPED_SCHEDULE_ACTIONS` si se
pueden programar, y sus claves de idioma a `HOST_SCOPED_TEXTS`.

## 11. Lo que NO entra en la fase 3

Para que el alcance no se mueva:

- Nada de `/deploy` ni `/backup`. Solo la capa que usan.
- Nada de editar el compose desde Telegram.
- No se toca el `/compose` actual, que **reconstruye** el fichero desde el
  inspect del contenedor (`compose_generator.py`). Poder leer el real abre la
  puerta a reemplazarlo, y es una decisión aparte: el reconstruido funciona
  para contenedores que no vienen de compose, y el real no existe para ellos.
- Ni cifrado ni rotación de backups.
