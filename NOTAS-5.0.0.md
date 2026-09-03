# docker-controller-bot 5.0.0 — estado y traspaso

Documento de continuidad. Recoge dónde está la 5.0.0, qué reglas no se pueden
romper, y qué queda abierto. Escrito el 2026-09-03.

Este fichero no es documentación del proyecto: es una nota de trabajo. Bórralo
o añádelo a `.gitignore` antes de publicar.

---

## 1. Situación

| | |
|---|---|
| Rama | `feature/v5.0.0` — **nada va a `main` hasta publicar** |
| `main` | sigue en `02376e2` (v4.2.0), intacta |
| `VERSION` en `core.py` | `5.0.0_fase2` |
| `ARG VERSION` en `Dockerfile` | `4.2.0`, **a propósito** (selecciona el tag de GitHub que se descarga; se mueve solo al publicar) |
| Tests | `python3 tests/run_all.py` → **178/178 OK** |
| Nada pusheado | el trabajo vive en local |

### Fases

1. ✅ **`/settings`** — capa de configuración y estado, `/start` con botones.
2. ✅ **Multi-host** — registro de hosts, referencias `<hostId>:<shortId>`, ssh/tcp/TLS.
   Auditoría cerrada.
3. ⏸️ **Cimiento de rutas** — aparcado con la fase 4. Diseño razonado en
   `PLAN-FASE-3.md`, sin una línea de código.
4. ⏸️ **`/deploy` y `/backup`** — aparcados el 2026-09-03.

**La 5.0.0 son las fases 1 y 2.** Las dos que quedaban se cayeron por sus
propios motivos, no por falta de tiempo: `/backup` por Telegram choca con que
la API de bots no envía documentos de más de 50 MB y un volumen nombrado son
gigas, y `/deploy` convierte al bot en un segundo escritor de un
`docker-compose.yml` que ya tiene otra fuente de verdad. El cimiento de rutas
cae con ellas porque no tenía propósito propio.

El orden 1 → 2 sí era una dependencia real y se respetó: `/settings` nació con
esquema anidado para que las fases siguientes escribieran en él sin volver a
migrar el fichero. Ese esquema aguanta igual si algún día se retoman.

### Commits de la rama (nuevo → viejo)

```
4b471a9  Quitar un host avisa de las programaciones que se lleva por delante
99e5821  Un mensaje de una sola máquina dice cuál
b43a2f5  Un mensaje que no llegó a enviarse ya no se usa como si existiera
3718033  Un cliente descartado se cierra, no solo se olvida
adfa5f6  /updateall vuelve a ver la flota entera, y el lint que debía notarlo
ec47722  Repaso de seguridad, tiempos de espera y navegación de los menús
7c6bc2e  Una programación de mute no pertenece a ningún host
2677d77  El host se pregunta donde el resumen lo enumera, y cierra la fase 2
02b3ba2  Las programaciones muestran y preguntan su host
0ce21ec  Enruta por host toda la ruta de actualización y las acciones de proyecto
7aa9453  El botón de actualizar de /checkupdate ya lleva su host
ff1f220  Un corte de red al reconocer una pulsación ya no anula la acción
f410b3c  Documenta cómo añadir el segundo y el tercer servidor por ssh
15a0492  La pantalla de elegir host pregunta solo por el host
f8775fd  Fase 2: cierra el multi-host en comandos, actualizaciones y programaciones
67bd94d  Arregla que el menú de multi-selección se repintara con el host local
5adb283  Fase 2: los selectores ofrecen contenedores de todos los hosts
```

---

## 2. Mapa de módulos

`docker-controller-bot.py` (15 líneas) es solo el punto de entrada: importa
`core`, `commands`, `callbacks` y llama a `core.main()`. El fichero original se
renombró a `core.py` con `git mv` para conservar el historial.

| Módulo | Líneas | Papel |
|---|---|---|
| `core.py` | 6834 | El núcleo compartido: `DockerManager`, monitores, teclados, cachés, UI de settings/start/hosts, dispatcher |
| `callbacks.py` | 1278 | 66 handlers `cb_*` (prefijados `core.`) + la factoría `register_project_navigation` |
| `commands.py` | 209 | Los 21 `cmd_*`, se registran vía `register_command` |
| `host_registry.py` | 587 | Los hosts: caché de clientes, `ping`, `drop`, `add_host`, `remove_host`, `rename_host`, `status_snapshot` |
| `store.py` | 511 | Almacenamiento: `get`/`set` por ruta punteada, estado, caché de updates, `batch()` |
| `migration.py` | 254 | Migraciones de arranque, 4.x → 5.0 |
| `callback_registry.py` | 150 | `CallbackSpec`, `Context`, decorador `callback()`, `parse()` |
| `config.py` | 141 | Solo variables de arranque + constantes |
| `i18n.py` | 64 | `language()`, `load_locale`, `get_text` |
| Preexistentes de 4.x | | `docker_update.py`, `compose_generator.py`, `docker_compose_manager.py`, `port_manager.py`, `schedule_manager.py`, `message_queue.py`, `logger.py` |

`commands.py` se importa por su efecto: rellena `COMMAND_ACTIONS`. **`core` no
importa `commands`** — la dependencia es de un solo sentido, a propósito.

---

## 3. Invariantes que no se pueden romper

Esto es lo que hay que releer antes de tocar cualquier cosa.

### La regla de oro del multi-host

> **Con un solo host, el nivel de host no aparece en ningún sitio.** La salida
> de `/list` tiene que ser idéntica byte a byte a la de antes de los hosts.

`host_label(host_id)` devuelve `""` cuando hay un host, y `<b>alias</b> · `
cuando hay varios. Los selectores se saltan la pantalla de host cuando solo uno
califica.

### Referencias de contenedor

La identidad que viaja en `callback_data` y en las cachés de mensajes es
`<hostId>:<shortId>` — p. ej. `h_6eb4:ede1e`.

```python
make_ref(host_id, container_id)   # construye
container_ref(host_id, container) # construye desde un objeto
parse_ref(ref)  -> (host_id, short_id)   # un id suelto = host local (compatibilidad)
ref_host(ref) / ref_id(ref)
manager_for(ref) -> DockerManager
find_container(ref) -> (manager, container) | (None, None)
```

**La referencia completa nunca puede llegar a la API de Docker.** El fallo
`No such container: h_1919:be866` fue exactamente eso. Antes de llamar al SDK
hay que resolver `host_id` y quedarse con el id corto.

Tres usos de `docker_manager.*` (el local) son deliberados y están comentados
como tales: `delete_updater`, `check_CONTAINER_NAME` /
`get_container_id_by_name`, y `get_my_architecture`. Cualquier otro es un bug.

Los hashes de proyecto llevan el host **dentro de la cadena hasheada**, no solo
en el valor guardado.

### Programaciones

`HOST_SCOPED_SCHEDULE_ACTIONS = {"run","stop","restart","exec","prune"}` en
`config.py`. `mute` **no** lleva host: son las notificaciones del propio bot.

Los tres renderizadores (`_build_schedule_summary`, `show_schedule_menu`,
`show_schedule_edit_options`) colocan la línea de host en el mismo sitio: tras
lo que la tarea toca, antes de `show_output`. Y la **pregunta** sigue ese mismo
orden, para que el resumen solo crezca hacia abajo. Hay un test que lo fija.

### Otras

- `callback_data` de Telegram: máximo **64 bytes**.
- `answer_callback_quietly()` en vez de `answer_callback_query()` — un corte de
  red al reconocer la pulsación no puede anular la acción. Hay un lint que lo
  vigila.
- Los emoji llevan su selector de variación **U+FE0F** cuando lo necesitan
  (`⏱️ ⏹️ 🗑️ 🏷️`). Dos lints lo comprueban. `🖧` (U+1F5A7) no tiene
  presentación emoji: no usarlo.
- Toda pantalla de menú lleva **Volver y Cerrar**, en fila propia, por la misma
  función. Dos excepciones: el selector de idioma del primer arranque y el
  diálogo de confirmación de borrar host.
- Escritura atómica: `flush` + `fsync` + `os.replace`.
- Todo lo que viene de fuera se escapa antes de ir a un mensaje (el bot manda
  HTML). `host_alias()` escapa por su cuenta.

### Convenciones de repo

- **Los commits no llevan `Co-Authored-By` ni ninguna atribución a
  Claude/Anthropic.** Tampoco los tags, PRs ni notas de versión.
- Mensajes de commit en español, en prosa, explicando el *por qué*. Título
  corto sin punto final.
- Los tests se ejecutan con `python3 tests/run_all.py`. El orden de `MODULES`
  importa (estado mutable de módulo): `test_lint`, `test_store`,
  `test_migration`, `test_hosts`, `test_bot`, `test_monitors`.
- `tests/data/callbacks_4.2.0.json` es la línea base congelada de callbacks;
  los cambios deliberados se anotan en `_deliberate_changes`.

---

## 4. La auditoría, cerrada

Los cinco hallazgos y el barrido pendiente están arreglados, cada uno con su
test, y comprobado que el test falla antes del arreglo. Se queda escrito qué
era cada cosa porque los lints que ahora lo vigilan solo se entienden sabiendo
qué dejaron pasar.

| | Era | Lo vigila |
|---|---|---|
| 4.1 | `/updateall` solo listaba el host local, y sus botones llevaban id suelto | `test_container_buttons_never_carry_a_bare_id` |
| 4.2 | Ese lint leía solo comillas dobles y eximía `{cid}` por el nombre | el propio lint, ahora sin los dos agujeros |
| 4.3 | `drop()` olvidaba los clientes sin cerrarlos → fuga de procesos ssh | `test_dropping_a_client_closes_it` y dos más |
| 4.4 | `x.message_id` sin comprobar que el envío salió | `test_a_message_that_was_never_sent_is_not_dereferenced` |
| 4.5 | `/version` y `/donate` dormían 15 y 45 s en un worker de telebot | `test_a_self_destructing_message_does_not_park_a_worker` |
| 4.6a | 44 mensajes de una sola máquina sin decir cuál | `test_a_message_about_one_host_says_which` |
| 4.6b | Quitar un host dejaba programaciones apuntando a la nada | `test_removing_a_host_warns_about_the_schedules_it_would_orphan` |
| 4.6c | El orden de locks core → host_registry no estaba fijado | `test_the_module_that_locks_last_depends_on_nothing_above_it` |
| idiomas | Marcado, enlaces y ejemplos de cron derivados en 7 de 8 locales | `test_a_translation_keeps_the_markup_of_the_original` y tres más |

Cosas que aprendí por el camino y conviene no volver a descubrir:

- Las cachés de `host_registry` guardan `(url, cliente)`, no el cliente. El
  primer intento de 4.3 pasaba la tupla y el `except` se lo comía en silencio.
- `delete_message(message_id)` resuelve el chat con `get_reply_chat_id()`, que
  es **thread-local**. Cualquier borrado diferido tiene que capturar el chat al
  programarse o acabará en `TELEGRAM_GROUP`.
- Un `/updateall` y el demonio de actualización tienen que compartir el
  renderizador de teclado (`build_generic_keyboard`). Montarlo a mano en los
  dos sitios es cómo el prefijo de host apareció solo a partir del segundo
  toque.
- Reescribir un `locale/*.json` con `json.dumps` reformatea el fichero entero.
  Insertar las claves como texto, respetando la indentación de dos espacios.

### Los idiomas

Cuatro tests cubren lo mecánico, que es lo que se rompe sin que nadie lo note:

- `test_a_translation_keeps_the_markup_of_the_original` cuenta las etiquetas
  HTML de cada clave contra el español. Las tres excepciones legítimas están
  en `MARKUP_EXCEPTIONS` con su motivo: el selector de idioma del primer
  arranque es bilingüe en español a propósito, el alemán parte las cursivas
  por el orden de palabras, y el traductor neerlandés se acredita con un
  enlace extra.
- `test_the_cron_examples_survive_in_every_language` fija el caso concreto:
  ese texto es la única ayuda de sintaxis cron que ve el usuario.
- `test_no_link_is_broken` prohíbe espacios dentro de un `href` y enlaces en
  Markdown, que el bot manda como HTML.
- `test_no_locale_invents_or_loses_a_link` compara las URL de cada clave con
  las del español, salvo en `version`, donde el traductor se acredita.

**Lo que no es lintable es la calidad del idioma.** Un detector de
castellanismos por expresión regular daba 105 falsos positivos en catalán
(`el`, `del`, `una`, `Eliminar` son catalán perfectamente válido) y marcaba
como error 76 cadenas gallegas que coinciden con el español porque en gallego
se dicen igual. Eso hay que leerlo. Si tocas `gl` o `cat`, lo que queda
idéntico al español está revisado una a una y es correcto: no lo "arregles"
en bloque.

### La regla nueva del barrido

Las claves de idioma que nombran un contenedor, un proyecto o un servicio
tienen que llegar al usuario con `host_label(...)` delante. La lista vive en
`HOST_SCOPED_TEXTS` en `tests/test_lint.py`; **una clave nueva de ese tipo se
añade ahí**. Los mensajes de progreso sin identidad propia (`loading_file`,
`fetching_image_data`) quedan fuera a propósito: no dicen de qué hablan y se
borran acto seguido. La única excepción está anotada con su motivo en
`NO_HOST_TO_NAME`.

Donde una función manda varios de esos mensajes, la etiqueta se calcula una
vez arriba en una variable `label`. El lint acepta `{label}` como equivalente
a `host_label`.

---

## 5. Cosas conocidas y aceptadas

No son bugs a arreglar ahora; están decididas.

- Editar `settings.json` a mano no se aplica hasta reiniciar (hay caché en
  memoria, nadie llama a `store.reload()`). Documentado en los dos README.
- `store.set` guarda **el objeto del llamante por referencia**. Los tests que
  comparten `HOST_FIXTURE` necesitan `copy.deepcopy`.
- `tcp://` sin TLS no tiene autenticación ninguna. Documentado con aviso; no se
  expone a internet.
- La clave ssh que usa el bot no puede llevar passphrase.
- Traducciones: los ocho idiomas están completos y revisados. Lo que había
  no era solo prosa floja: **siete de los ocho** llevaban el ejemplo de cron
  de `error_adding_schedule` destrozado (`0 1 * * *` convertido en
  `0 1 <b> </b> <b>`, los asteriscos comidos por una conversión de Markdown),
  `de/en/nl` tenían las URL de DockerHub y GitHub con un espacio dentro —
  enlaces muertos—, `nl` escribía uno de ellos en Markdown cuando el bot manda
  HTML, `ru` había perdido la línea de uso de `exec`, `de` la palabra `CRON`,
  y `nl` traducía "minutes" como `notulen` (actas de una reunión). El gallego
  llevaba 16 castellanismos y 14 `mais` sin acento; el catalán, 60 —entre
  ellos nueve `¿` invertidas, que en catalán no existen, y `comando` por
  `comanda`. Cuatro tests nuevos lo fijan; ver sección 4.
- Ofrecido y sin decidir: renombrar "Mensajes ampliados" y "Selección múltiple"
  a algo autoexplicativo; documentar Tailscale; meter los tests en CI.

---

## 6. Seguridad — el modelo actual

- **En `settings.json` no vive ninguna credencial.** Solo URLs y rutas a
  certificados. Una clave ssh o un certificado TLS se mapean como fichero.
- `~/.ssh` se mapea **en solo lectura** dentro del contenedor.
- Los pasos de ssh se hacen en la máquina del bot, **no dentro del
  contenedor**: el bot no puede aceptar una huella nueva por ti, no hay
  terminal donde responder que sí, y el `.ssh` es de solo lectura.
- `add_host` valida el esquema (`unix://`, `tcp://`, `ssh://` y nada más) y
  rechaza duplicados al mismo daemon. `HostRejected` lleva el motivo en un
  campo, no en un texto que haya que interpretar.
- La URL llega tecleada por el admin y va a `DockerClient(base_url=...)`.
  docker-py construye `['ssh', ..., '--', host, 'docker system dial-stdio']`
  como lista, sin shell, y el `--` corta la inyección de opciones. Revisado.
- Todo lo que viene de fuera se escapa antes de ir a un mensaje HTML: logs de
  contenedor, información de proyecto, alias de host, URL recién tecleada,
  motivo de un fallo de conexión.

---

## 7. Entorno de pruebas

Tres hosts reales, útiles para verificar en vivo (39 contenedores en total):

| id | alias | url |
|---|---|---|
| `h_cf94` | MacBook Pro | `unix:///var/run/docker.sock` (local) |
| `h_6eb4` | Ganimedes | `ssh://root@ganimedes.lan` |
| `h_1919` | Jupiter | `tcp://jupiter.lan:2375` |

El bot corre en el contenedor `docker-controller-bot`. Los ajustes están en
`/app/schedule/settings.json` dentro del contenedor.

**`docker compose restart` no aplica cambios de volúmenes** — hace falta
`docker compose up -d`. Eso fue lo que provocó el `BrokenPipeError` de ssh
aquella vez.

Imagen: Alpine con `py3-paramiko` y `openssh-client` por **apk, no pip**
(+10–13 MB). `cryptography` no tiene rueda musl para armv7 y pip lo compilaría
con Rust.

---

## 8. Por dónde seguir

Con las fases 3 y 4 fuera, **la rama está lista para publicar**. Lo que queda
es el ritual:

- `VERSION` en `core.py`: `5.0.0_fase2` → `5.0.0`.
- `ARG VERSION` del `Dockerfile`: `4.2.0` → el tag real de la 5.0.0.
- Borrar o `.gitignore` estas notas y `PLAN-FASE-3.md`.
- Los dos README: repasar que no prometan `/deploy` ni `/backup`.
- Merge a `main`, que sigue intacta en `02376e2`.

Aparte del ritual, hay una mejora que **no** depende de nada aparcado y que se
sostiene sola: hoy `/compose` reconstruye el fichero desde el inspect del
contenedor y el propio bot se disculpa por ello en el texto («*se encuentra en
fase experimental y puede contener errores*»). Leer el `docker-compose.yml` de
verdad no necesita ni el plugin de compose, ni el socket montado en un
auxiliar, ni descargar imagen: basta crear un contenedor con el directorio
montado **en solo lectura**, usando una imagen que el host ya tenga. Esquiva
cinco de los seis riesgos del plan aparcado. Sin decidir.
