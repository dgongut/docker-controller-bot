"""
Handlers for the inline buttons.

One function per callback, each declaring beside itself what the dispatcher
needs to know: the arguments it carries, whether it repaints its own message,
whether its argument is a hashed project name. Those facts used to sit in four
dictionaries in config.py, which is how a handler could be complete and
correct and still do nothing.

Everything these handlers act on lives in the core, and they say so at every
use rather than in one import block: at 89 names, a block would hide the
coupling instead of showing it.

Importing this module is what registers the callbacks, so the entry point has
to import it before polling starts.
"""

import html

from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup

import core
import host_registry
import store
from callback_registry import callback
from callback_registry import register as register_callback
from config import SUPPORTED_LANGUAGES
from i18n import get_text


#
# One function per callback, each declaring beside itself what the dispatcher
# needs to know: the arguments it carries, whether it repaints its own message,
# whether its argument is a hashed project name. Those facts used to sit in
# four dictionaries in config.py, which is how a handler could be complete and
# correct and still do nothing.

@callback(
	name='run',
	params=('containerId',),
	multi_action=True,
)
def cb_run(ctx):
	result = core.run(ctx.containerId, ctx.containerName)
	if ctx.multiAction:
		core.refresh_multi_action_menu(ctx.chatId, ctx.messageId, [ctx.containerName], succeeded=result is None)

@callback(
	name='stop',
	params=('containerId',),
	multi_action=True,
)
def cb_stop(ctx):
	result = core.stop(ctx.containerId, ctx.containerName)
	if ctx.multiAction:
		core.refresh_multi_action_menu(ctx.chatId, ctx.messageId, [ctx.containerName], succeeded=result is None)

@callback(
	name='restart',
	params=('containerId',),
	multi_action=True,
)
def cb_restart(ctx):
	result = core.restart(ctx.containerId, ctx.containerName)
	if ctx.multiAction:
		core.refresh_multi_action_menu(ctx.chatId, ctx.messageId, [ctx.containerName], succeeded=result is None)

@callback(
	name='logs',
	params=('containerId',),
)
def cb_logs(ctx):
	core.logs(ctx.containerId, ctx.containerName)

@callback(name="logsPause", params=("containerId",), keeps_message=True)
def cb_logsPause(ctx):
	core.log_followers.pause(ctx.containerId, ctx.containerName, ctx.chatId, ctx.messageId)

@callback(name="logsFollow", params=("containerId",), keeps_message=True)
def cb_logsFollow(ctx):
	core.log_followers.follow(ctx.containerId, ctx.containerName, ctx.chatId, ctx.messageId)

@callback(
	name='logfile',
	params=('containerId',),
)
def cb_logfile(ctx):
	core.log_file(ctx.containerId, ctx.containerName)

@callback(
	name='compose',
	params=('containerId',),
)
def cb_compose(ctx):
	core.compose(ctx.containerId, ctx.containerName)

@callback(
	name='info',
	params=('containerId',),
)
def cb_info(ctx):
	core.info(ctx.containerId, ctx.containerName)

@callback(
	name='infoSetUpdates',
	params=('value', 'containerId'),
	keeps_message=True,
	answer_immediately=False,
)
def cb_infoSetUpdates(ctx):
	"""
	Sets what to do with a container's new versions —the next one round, the
	button says which— and repaints its /info buttons showing it.
	"""
	if ctx.value not in core.UPDATE_MODE_CODES:
		core.answer_callback_quietly(ctx.call.id)
		return
	owner, container = core.find_container(ctx.containerId)
	if container is None:
		core.answer_callback_quietly(ctx.call.id)
		return
	refused = core.set_update_mode(owner.host_id, container.name, container.labels,
								   core.container_image(container), core.UPDATE_MODE_CODES[ctx.value])
	core.repaint_info(ctx.call.id, ctx.chatId, ctx.messageId, ctx.containerId, alert=refused, found=(owner, container))

@callback(
	name='confirmUpdate',
	params=('containerId',),
)
def cb_confirmUpdate(ctx):
	core.confirm_update(ctx.containerId, ctx.containerName)

@callback(
	name='checkUpdate',
	params=('containerId',),
)
def cb_checkUpdate(ctx):
	core.manager_for(ctx.containerId).force_check_update(core.ref_id(ctx.containerId))

@callback(
	name='update',
	params=('containerId',),
)
def cb_update(ctx):
	core.update_container(ctx.containerId, ctx.containerName)

@callback(
	name='updateAll',
)
def cb_updateAll(ctx):
	"""
	Updates everything with a pending update, on every reachable host.

	Host by host in sequence, because updating pulls images and doing several
	machines at once would fight over the same network.
	"""
	core.update_containers([tuple(pair) for pair in core.pending_updates()])

@callback(
	name='confirmDelete',
	params=('containerId',),
)
def cb_confirmDelete(ctx):
	core.confirm_delete(ctx.containerId, ctx.containerName)

@callback(
	name='askCommand',
	params=('containerId',),
)
def cb_askCommand(ctx):
	core.ask_command(ctx.userId, ctx.containerId, ctx.containerName)

@callback(
	name='exec',
	params=('containerId', 'commandId'),
)
def cb_exec(ctx):
	command = core.take_command_cache(ctx.commandId)
	if command is not None:
		core.execute_command(ctx.containerId, ctx.containerName, command)
	else:
		core.error(f"Command cache not found for ID: {ctx.commandId}")
		core.send_message(message=get_text("error_callback_processing"))

@callback(
	name='cancelAskCommand',
)
def cb_cancelAskCommand(ctx):
	core.clear_command_request_state(ctx.userId)

@callback(
	name='cancelExec',
	params=('commandId',),
)
def cb_cancelExec(ctx):
	core.clear_command_cache(ctx.commandId)

@callback(
	name='delete',
	params=('containerId',),
)
def cb_delete(ctx):
	label = core.host_label(ctx.hostId)
	x = core.send_message(message=f'{label}{get_text("deleting", ctx.containerName)}')
	result = core.manager_for(ctx.containerId).delete(
		container_id=core.ref_id(ctx.containerId), container_name=ctx.containerName)
	if x:
		core.delete_message(x.message_id)
	# delete() builds its message from the short id alone; the host is known
	# here, so this is where it gets said.
	core.send_message(message=f'{label}{result}')

@callback(
	name='changeTagContainer',
	params=('containerId',),
)
def cb_changeTagContainer(ctx):
	# The dispatcher has already named it, or answered for it.
	core.change_tag_container(ctx.containerId, ctx.containerName)

@callback(
	name='confirmChangeTag',
	params=('containerId', 'tag'),
)
def cb_confirmChangeTag(ctx):
	# The dispatcher has already named it, or answered for it.
	core.confirm_change_tag(ctx.containerId, ctx.containerName, ctx.tag)

@callback(
	name='changeTag',
	params=('containerId', 'tag'),
)
def cb_changeTag(ctx):
	# The dispatcher has already named it, or answered for it.
	core.update_container(ctx.containerId, ctx.containerName, tag=ctx.tag)

@callback(
	name='toggleUpdate',
	params=('containerId',),
	keeps_message=True,
	answer_immediately=False,
)
def cb_toggleUpdate(ctx):
	with core.update_data_lock:
		containers, selected = core.load_update_data(ctx.chatId, ctx.messageId)
		if not containers:
			core.expire_update_list(ctx.call.id, ctx.chatId, ctx.messageId)
			return
		reference = _listed_reference(containers, ctx)
		if reference is None:
			core.expire_update_list(ctx.call.id, ctx.chatId, ctx.messageId)
			return
		if reference in selected:
			selected.remove(reference)
		else:
			selected.add(reference)
		# The page the tapped button is on, which is the one being looked at.
		page = core.page_of(containers, reference)
		core.save_update_data(ctx.chatId, ctx.messageId, containers, selected, page)

	markup = core.build_generic_keyboard(containers, selected, ctx.messageId, "Update", get_text("button_update"), get_text("button_update_all"), page)

	# Use synchronous edit for immediate feedback
	try:
		core.edit_message_reply_markup_sync(ctx.chatId, ctx.messageId, reply_markup=markup)
		# Answer callback without text (no annoying popup)
		core.answer_callback_quietly(ctx.call.id)
	except Exception as e:
		core.error(f"Error updating toggle: {e}")
		core.answer_callback_quietly(ctx.call.id)

def _listed_reference(containers, ctx):
	"""
	The reference the list holds for the pressed button, or None.

	The dispatcher points a button at the live container when the one it was
	made for has been recreated since, which is right for acting on it and
	wrong here: the list still holds the old reference, so the toggle matched
	nothing. Found by name on the same host instead.
	"""
	for cid, cname in containers:
		if cid == ctx.containerId:
			return cid
	for cid, cname in containers:
		if cname == ctx.containerName and core.ref_host(cid) == core.ref_host(ctx.containerId):
			return cid
	return None

@callback(
	name='toggleUpdateAll',
	keeps_message=True,
	answer_immediately=False,
)
def cb_toggleUpdateAll(ctx):
	with core.update_data_lock:
		containers, selected = core.load_update_data(ctx.chatId, ctx.messageId)
		if not containers:
			core.expire_update_list(ctx.call.id, ctx.chatId, ctx.messageId)
			return
		selected.update(cid for cid, _cname in containers)
		page = core.load_update_page(ctx.chatId, ctx.messageId)
		core.save_update_data(ctx.chatId, ctx.messageId, containers, selected, page)

	markup = core.build_generic_keyboard(containers, selected, ctx.messageId, "Update", get_text("button_update"), get_text("button_update_all"), page)

	# Use synchronous edit for immediate feedback
	try:
		core.edit_message_reply_markup_sync(ctx.chatId, ctx.messageId, reply_markup=markup)
		# Answer callback without text (no annoying popup)
		core.answer_callback_quietly(ctx.call.id)
	except Exception as e:
		core.error(f"Error updating toggle all: {e}")
		core.answer_callback_quietly(ctx.call.id)

@callback(
	name='pageUpdate',
	params=('value',),
	keeps_message=True,
	answer_immediately=False,
)
def cb_pageUpdate(ctx):
	"""Shows another page of an update list too long for one keyboard."""
	with core.update_data_lock:
		containers, selected = core.load_update_data(ctx.chatId, ctx.messageId)
		if not containers:
			core.expire_update_list(ctx.call.id, ctx.chatId, ctx.messageId)
			return
		page, _pages = core.update_keyboard_page(containers, ctx.value)
		core.save_update_data(ctx.chatId, ctx.messageId, containers, selected, page)

	markup = core.build_generic_keyboard(containers, selected, ctx.messageId, "Update", get_text("button_update"), get_text("button_update_all"), page)
	try:
		core.edit_message_reply_markup_sync(ctx.chatId, ctx.messageId, reply_markup=markup)
	except Exception as e:
		# Tapping the counter repaints the same keyboard, which Telegram
		# answers with "message is not modified". Nothing to report.
		core.debug(f"Could not change the update list page: {e}")
	core.answer_callback_quietly(ctx.call.id)

@callback(
	name='confirmUpdateSelected',
	params=('originalMessageId',),
)
def cb_confirmUpdateSelected(ctx):
	core.confirm_update_selected(ctx.chatId, ctx.messageId)

@callback(
	name='updateSelected',
	params=('originalMessageId',),
)
def cb_updateSelected(ctx):
	# Taken, not read: a second tap on "confirm" finds nothing and runs nothing.
	containers, selected = core.take_update_data(ctx.chatId, ctx.originalMessageId)
	if not containers or not selected:
		core.send_message(message=get_text("update_list_expired"))
		return
	# Everything ticked ends up in the summary. What was not found or had
	# nothing left to update used to be dropped before the batch, with at most
	# a loose "does not exist" naming an ID, and the count left it out.
	names = {entry[0]: entry[1] for entry in containers}
	targets, up_to_date, missing = [], [], []
	for ref in core.selected_in_order(containers, selected):
		# Each selection carries its own host: an /updateall list can span
		# machines, so they cannot all be looked up on the local one.
		host_id, name = core.ref_host(ref), names[ref]
		_owner, container = core.find_live_container(ref, name)
		if container is None:
			core.debug(f"Container {ref} ({name}) not found")
			missing.append((host_id, name))
		elif core.update_available(container, host_id):
			targets.append((core.container_ref(host_id, container), container.name))
		else:
			up_to_date.append((host_id, container.name))
	core.update_containers(targets, up_to_date=up_to_date, missing=missing)

@callback(
	name='restartWholeProject',
	params=('containerName',),
	project_arg=True,
	multi_action=True,
)
def cb_restartWholeProject(ctx):
	project_name = ctx.containerName
	# Captured before acting so every service can be marked as done
	project_container_names = core.get_project_container_names(project_name, ctx.hostId) if ctx.multiAction else None
	core.restart_compose_project(project_name, ctx.hostId)
	if ctx.multiAction:
		core.refresh_multi_action_menu(ctx.chatId, ctx.messageId, project_container_names)

@callback(
	name='runWholeProject',
	params=('containerName',),
	project_arg=True,
	multi_action=True,
)
def cb_runWholeProject(ctx):
	project_name = ctx.containerName
	# Captured before acting so every service can be marked as done
	project_container_names = core.get_project_container_names(project_name, ctx.hostId) if ctx.multiAction else None
	core.run_compose_project(project_name, ctx.hostId)
	if ctx.multiAction:
		core.refresh_multi_action_menu(ctx.chatId, ctx.messageId, project_container_names)

@callback(
	name='stopWholeProject',
	params=('containerName',),
	project_arg=True,
	multi_action=True,
)
def cb_stopWholeProject(ctx):
	project_name = ctx.containerName
	# Captured before acting so every service can be marked as done
	project_container_names = core.get_project_container_names(project_name, ctx.hostId) if ctx.multiAction else None
	core.stop_compose_project(project_name, ctx.hostId)
	if ctx.multiAction:
		core.refresh_multi_action_menu(ctx.chatId, ctx.messageId, project_container_names)

@callback(
	name='enterComposeProject',
	params=('containerName',),
	keeps_message=True,
	project_arg=True,
)
def cb_enterComposeProject(ctx):
	project_name = ctx.containerName
	project_info = core.project_info_or_none(ctx.hostId, project_name)

	if not project_info:
		core.send_message(message=f'{get_text("error_project_not_found", html.escape(str(project_name)))}{core.host_suffix(ctx.hostId)}')
		return

	# Build Level 2 keyboard
	markup = InlineKeyboardMarkup(row_width=core.button_columns())
	botones = []

	# Add individual container buttons (sorted by status and service name)
	for service_name in core.sort_project_services(project_info):
		container = project_info.services[service_name]
		status_emoji = core.get_status_emoji(container.status, container.name, container, ctx.hostId)
		botones.append(
			InlineKeyboardButton(
				f"{status_emoji} {service_name}",
				callback_data=f"compose|{core.container_ref(ctx.hostId, container)}"
			)
		)

	markup.add(*botones)

	# Add back button
	markup.add(
		InlineKeyboardButton(
			get_text("button_back"),
			callback_data="backToComposeLevel1"
		)
	)

	# Save container cache for this project
	core.save_container_cache(ctx.chatId, ctx.messageId, project_info.containers, ctx.hostId)

	core.edit_message_text(
		f'{get_text("select_container_from_project", html.escape(str(project_name)))}'
		f'{core.host_suffix(ctx.hostId)}',
		ctx.chatId,
		ctx.messageId,
		reply_markup=markup
	)

@callback(
	name='showProjectInfo',
	params=('containerName',),
	keeps_message=True,
	project_arg=True,
)
def cb_showProjectInfo(ctx):
	project_name = ctx.containerName

	# Get formatted project info
	info_text = core.manager(ctx.hostId).get_project_info_formatted(project_name)

	# Build keyboard with close button
	markup = InlineKeyboardMarkup(row_width=1)
	markup.add(
		InlineKeyboardButton(
			get_text("button_close"),
			callback_data="cerrar"
		)
	)

	core.edit_message_text(
		info_text,
		ctx.chatId,
		ctx.messageId,
		reply_markup=markup
	)

@callback(
	name='confirmDeleteWholeProject',
	params=('containerName',),
	keeps_message=True,
	project_arg=True,
)
def cb_confirmDeleteWholeProject(ctx):
	project_name = ctx.containerName
	project_info = core.project_info_or_none(ctx.hostId, project_name)

	if not project_info:
		core.send_message(message=f'{get_text("error_project_not_found", html.escape(str(project_name)))}{core.host_suffix(ctx.hostId)}')
		return

	# What will actually go: the bot is left out of a whole-project delete.
	going = core.without_the_bot(ctx.hostId, project_info.containers)[0]
	container_count = len(going)
	settings_note = (get_text("confirm_delete_settings")
					 if any(store.container_update_mode(ctx.hostId, c.name) for c in going) else "")
	markup = InlineKeyboardMarkup(row_width=2)
	markup.add(
		InlineKeyboardButton(
			f"✅ {get_text('button_yes_delete')}",
			callback_data=f"deleteWholeProject|{core.register_project_hash(project_name, ctx.hostId)}"
		),
		InlineKeyboardButton(
			get_text('button_cancel'),
			callback_data="backToDeleteLevel1"
		)
	)
	core.edit_message_text(
		f'{core.host_label(ctx.hostId)}'
		f'{get_text("confirm_delete_project", html.escape(str(project_name)), container_count)}{settings_note}',
		ctx.chatId,
		ctx.messageId,
		reply_markup=markup
	)

@callback(
	name='deleteWholeProject',
	params=('containerName',),
	project_arg=True,
)
def cb_deleteWholeProject(ctx):
	project_name = ctx.containerName
	core.delete_compose_project(project_name, ctx.hostId)

@callback(name="portsHost", params=("value",))
def cb_portsHost(ctx):
	"""Shows one host's ports from the /ports host list."""
	core.show_container_ports(ctx.value)


@callback(name="pruneHost", params=("value",), keeps_message=True)
def cb_pruneHost(ctx):
	"""Steps into one host from the /prune host list."""
	core.render_prune_types(ctx.chatId, ctx.messageId, ctx.value)


@callback(
	name='prune',
	params=('action', 'value'),
)
def cb_prune(ctx):
	"""
	Confirms and runs a prune on one host.

	`action` is confirmPrune<Kind> or prune<Kind>, `value` is the host. Driving
	the four object types off one table rather than four near-identical
	branches, which is what this was.
	"""
	host_id = ctx.value or core.host_registry.local_host_id()
	labels = {
		"Containers": "button_containers",
		"Images": "button_images",
		"Networks": "button_networks",
		"Volumes": "button_volumes",
	}

	if ctx.action.startswith("confirmPrune"):
		kind = ctx.action[len("confirmPrune"):]
		if kind not in labels:
			core.warning(f"Unknown prune type: {ctx.action}")
			return
		core.confirm_prune(kind, host_id)
		return

	if not ctx.action.startswith("prune"):
		core.warning(f"Unknown prune action: {ctx.action}")
		return

	kind = ctx.action[len("prune"):]
	if kind not in labels:
		core.warning(f"Unknown prune type: {ctx.action}")
		return

	try:
		owner = core.manager(host_id)
	except core.host_registry.HostUnavailable as e:
		core.send_message(message=get_text("host_unreachable",
										core.host_alias(host_id), html.escape(str(e.reason))))
		return

	# A prune can take minutes, and the confirmation is already gone by now
	# (the dispatcher removes it): without this the chat went quiet until
	# Docker answered, and running /prune again only got "already running".
	working = core.send_message(message=f'{core.host_label(host_id)}'
								f'{get_text("prune_working", get_text(labels[kind]))}')
	result, data, _ = getattr(owner, f"prune_{kind.lower()}")()
	if working:
		core.delete_message(working.message_id)
	markup = core.create_simple_keyboard("button_delete")
	if data is None:
		# It failed: the message says so, and there is nothing to attach.
		core.send_message(message=result, reply_markup=markup)
		return
	fichero_temporal = core.get_temporal_file(data, get_text(labels[kind]))
	x = core.send_message(message=get_text("loading_file"))
	core.send_document(document=fichero_temporal, reply_markup=markup, caption=result)
	if x:
		core.delete_message(x.message_id)


@callback(
	name='generatePort',
	params=("value",),
)
def cb_generatePort(ctx):
	# Generate a random available port
	port = core.get_random_available_port(ctx.value)

	# Build the message with the generated port
	if port:
		result_message = get_text("ports_generated_port", port)
	else:
		result_message = get_text("ports_no_available_port")

	# Delete the original message and send a new one with the result
	core.delete_message(ctx.messageId, ctx.chatId)
	core.send_message(chat_id=ctx.chatId, message=result_message)

@callback(
	name='checkPort',
	params=("value",),
)
def cb_checkPort(ctx):
	# Ask user for port to check
	core.ask_port_to_check(ctx.userId, ctx.value)

@callback(
	name='cancelCheckPort',
)
def cb_cancelCheckPort(ctx):
	# Cancel port check request
	core.clear_port_check_request_state(ctx.userId)
	core.delete_message(ctx.messageId, ctx.chatId)

@callback(
	name='settings',
	keeps_message=True,
)
def cb_settings(ctx):
	core.render_settings(ctx.chatId, ctx.messageId)

@callback(
	name='settingsToggle',
	params=('field',),
	keeps_message=True,
)
def cb_settingsToggle(ctx):
	if ctx.field in core.SETTINGS_TOGGLES:
		store.toggle(f"bot.{ctx.field}")
		if ctx.field == "telemetry" and not store.get("bot.telemetry"):
			core.telemetry_client.forget()
		core.render_settings(ctx.chatId, ctx.messageId, core.SETTINGS_TOGGLE_SCREEN.get(ctx.field, "main"))
	else:
		core.warning(f"Ignored toggle of unknown setting: {ctx.field}")

@callback(
	name='settingsUpdates',
	keeps_message=True,
)
def cb_settingsUpdates(ctx):
	# Back here from a per-container list, which is not shown any more: a tap
	# still on its way only says so, instead of landing on what this shows.
	with core.container_list_lock:
		core.clear_container_list(ctx.chatId, ctx.messageId)
	core.render_settings(ctx.chatId, ctx.messageId, "updates")

@callback(
	name='settingsContainers',
	keeps_message=True,
)
def cb_settingsContainers(ctx):
	"""What the update check does with each container: the hosts, or the only list."""
	# Back here from a list, which is not shown any more.
	with core.container_list_lock:
		core.clear_container_list(ctx.chatId, ctx.messageId)
	core.show_settings_containers(ctx.chatId, ctx.messageId)

@callback(
	name='ctrHost',
	params=('value',),
	keeps_message=True,
)
def cb_ctrHost(ctx):
	"""One host's containers in the per-container settings."""
	# Paused since the picker was drawn: it is not asked anything, as the
	# pause promises, and the picker names it with what it keeps instead.
	if host_registry.host(ctx.value) is None or host_registry.is_paused(ctx.value):
		core.show_settings_containers(ctx.chatId, ctx.messageId)
		return
	core.show_settings_containers_list(ctx.chatId, ctx.messageId, ctx.value)

@callback(
	name='ctrToggle',
	params=('field', 'containerIdx', 'value'),
	keeps_message=True,
	answer_immediately=False,
)
def cb_ctrToggle(ctx):
	"""Moves a container of the list on to the mode its button says."""
	data = core.load_container_list(ctx.chatId, ctx.messageId, ctx.field)
	try:
		index = int(ctx.containerIdx)
	except (TypeError, ValueError):
		index = -1
	if data is None or not 0 <= index < len(data["items"]) or ctx.value not in core.UPDATE_MODE_CODES:
		core.expire_container_list(ctx.call.id)
		return
	refused = core.toggle_in_container_list(data, index, core.UPDATE_MODE_CODES[ctx.value])
	# The tap asked the daemon, which takes a moment. Back, another page or
	# another host may have been pressed meanwhile: the list as it is now is
	# what gets the refreshed item and the repaint, or nothing does.
	with core.container_list_lock:
		current = core.load_container_list(ctx.chatId, ctx.messageId, ctx.field)
		if (current is None or index >= len(current["items"])
				or current["items"][index][0] != data["items"][index][0]):
			core.expire_container_list(ctx.call.id)
			return
		current["items"][index] = data["items"][index]
		core.save_container_list(ctx.chatId, ctx.messageId, current)
	_text, markup = core.build_settings_containers_list(current)
	core.repaint_markup(ctx.call.id, ctx.chatId, ctx.messageId, markup, alert=refused)

@callback(
	name='ctrPage',
	params=('field', 'value'),
	keeps_message=True,
	answer_immediately=False,
)
def cb_ctrPage(ctx):
	"""Another page of a per-container list too long for one keyboard."""
	with core.container_list_lock:
		data = core.load_container_list(ctx.chatId, ctx.messageId, ctx.field)
		if data is None:
			core.expire_container_list(ctx.call.id)
			return
		data["page"], _pages = core.update_keyboard_page(data["items"], ctx.value)
		core.save_container_list(ctx.chatId, ctx.messageId, data)
	_text, markup = core.build_settings_containers_list(data)
	core.repaint_markup(ctx.call.id, ctx.chatId, ctx.messageId, markup)

@callback(
	name='settingsLanguage',
	keeps_message=True,
)
def cb_settingsLanguage(ctx):
	core.show_settings_language(ctx.chatId, ctx.messageId)

@callback(
	name='settingsSetLanguage',
	params=('value',),
	keeps_message=True,
)
def cb_settingsSetLanguage(ctx):
	if ctx.value in SUPPORTED_LANGUAGES:
		store.set("bot.language", ctx.value)
		# Telegram holds the command menu on its side, already
		# translated, so it has to be published again or it would stay
		# in the previous language.
		core.register_bot_commands()
		core.render_settings(ctx.chatId, ctx.messageId)
	else:
		core.warning(f"Ignored unsupported language: {ctx.value}")

@callback(
	name='settingsTelemetry',
	keeps_message=True,
)
def cb_settingsTelemetry(ctx):
	core.render_settings(ctx.chatId, ctx.messageId, "telemetry")

@callback(
	name='telemetryShow',
	keeps_message=True,
)
def cb_telemetryShow(ctx):
	# A message of its own, so whatever it was pressed on stays as it was.
	text, markup = core.build_telemetry_preview()
	core.send_message(message=text, reply_markup=markup)

@callback(
	name='telemetryAccept',
	keeps_message=True,
)
def cb_telemetryAccept(ctx):
	# The notice is gone, but 5.0.0_RC5 sent it with these two buttons, and a
	# press on an already sent one must not end in an error.
	core.edit_message_reply_markup(ctx.chatId, ctx.messageId, InlineKeyboardMarkup())

@callback(
	name='telemetryDisable',
	keeps_message=True,
)
def cb_telemetryDisable(ctx):
	core.disable_telemetry()
	core.edit_message_reply_markup(ctx.chatId, ctx.messageId, InlineKeyboardMarkup())
	core.send_message(message=get_text("telemetry_disabled"))

@callback(
	name='settingsColumns',
	keeps_message=True,
)
def cb_settingsColumns(ctx):
	core.show_settings_columns(ctx.chatId, ctx.messageId)

@callback(
	name='settingsSetColumns',
	params=('value',),
	keeps_message=True,
)
def cb_settingsSetColumns(ctx):
	try:
		store.set("bot.button_columns", max(1, min(int(ctx.value), 8)))
	except (TypeError, ValueError):
		core.warning(f"Ignored invalid button column count: {ctx.value}")
	core.render_settings(ctx.chatId, ctx.messageId)

@callback(
	name='settingsAskInterval',
)
def cb_settingsAskInterval(ctx):
	core.ask_text_input(ctx.userId, "check_update_every_hours", "settings_ask_interval", back_to="updates")

@callback(
	name='settingsAskChannel',
)
def cb_settingsAskChannel(ctx):
	core.ask_text_input(ctx.userId, "notification_channel", "settings_ask_channel", back_to="main")

@callback(
	name='settingsClearChannel',
	keeps_message=True,
)
def cb_settingsClearChannel(ctx):
	store.set("bot.notification_channel", "")
	core.render_settings(ctx.chatId, ctx.messageId)
	core.send_message(message=get_text("settings_channel_cleared"))

@callback(
	name='cancelTextInput',
)
def cb_cancelTextInput(ctx):
	pending = core.load_text_input_state(ctx.userId)
	core.clear_text_input_state(ctx.userId)
	if pending and pending.get("back_to"):
		core.send_settings_menu(screen=pending["back_to"])

@callback(
	name='startMenu',
	keeps_message=True,
)
def cb_startMenu(ctx):
	core.render_start_menu(ctx.chatId, ctx.messageId)

@callback(
	name='startCategory',
	params=('value',),
	keeps_message=True,
)
def cb_startCategory(ctx):
	core.render_start_category(ctx.chatId, ctx.messageId, ctx.value)

@callback(
	name='startCommand',
	params=('value',),
)
def cb_startCommand(ctx):
	ctx.action = core.COMMAND_ACTIONS.get(f"/{ctx.value}")
	if ctx.action is None:
		core.warning(f"Unknown start menu command: {ctx.value}")
	else:
		# The same command as typing it, so it counts as the same thing.
		core.count_usage(f"cmd_{ctx.value}")
		ctx.action(user_id=ctx.userId, chat_id=ctx.chatId)

# Project navigation is one shape repeated per action: a button that steps into
# a Compose project, and one that comes back out. Written by hand that was 22
# callbacks with two distinct bodies between them, each needing entries in
# several registries, so adding an action meant ten edits across two files.
#
# Compose is the exception and stays hand-written below: the generic keyboard
# builder redirects a stopped container's button to `run`, which for /compose
# would start the container instead of showing its compose file.
PROJECT_NAVIGATION_ACTIONS = (
	"Run", "Stop", "Restart", "Delete", "Exec",
	"Logs", "Logfile", "Info", "CheckUpdate", "ChangeTag",
)

# The actions whose menus stay open for multi-selection, and so have to keep
# the session pointed at the right level.
PROJECT_MULTI_SELECTION_ACTIONS = frozenset({"Run", "Stop", "Restart"})


def register_project_navigation(action):
	"""Registers the enter/back pair of callbacks for one action."""
	multi = action in PROJECT_MULTI_SELECTION_ACTIONS

	def enter(ctx):
		if multi:
			core.enter_project_multi_aware(action, ctx.containerName, ctx.chatId, ctx.messageId, ctx.hostId)
		else:
			core.handle_enter_project_level2(action, ctx.containerName, ctx.chatId, ctx.messageId,
											host_id=ctx.hostId)

	def back(ctx):
		# The button carries no reference, so the dispatcher can only guess the
		# local host; the message itself remembers which one it was showing.
		host_id = core.message_host(ctx.chatId, ctx.messageId) or ctx.hostId
		if multi:
			core.back_to_level1_multi_aware(action, ctx.chatId, ctx.messageId, host_id)
			return
		result = core.build_back_to_level1_keyboard(action, ctx.chatId, ctx.messageId, host_id=host_id)
		if result:
			markup, message_key = result
			core.edit_message_text(get_text(message_key), ctx.chatId, ctx.messageId, reply_markup=markup)

	enter.__name__ = f"cb_enter{action}Project"
	back.__name__ = f"cb_backTo{action}Level1"

	register_callback(f"enter{action}Project", enter, params=("containerName",),
					keeps_message=True, project_arg=True)
	register_callback(f"backTo{action}Level1", back, keeps_message=True)


for _action in PROJECT_NAVIGATION_ACTIONS:
	register_project_navigation(_action)


def _back_to_compose_level1(ctx):
	"""Compose has no generated enter, but its way back is everyone else's."""
	host_id = core.message_host(ctx.chatId, ctx.messageId) or ctx.hostId
	result = core.build_back_to_level1_keyboard("Compose", ctx.chatId, ctx.messageId, host_id=host_id)
	if result:
		markup, message_key = result
		core.edit_message_text(get_text(message_key), ctx.chatId, ctx.messageId, reply_markup=markup)


register_callback("backToComposeLevel1", _back_to_compose_level1, keeps_message=True)


@callback(name="pickHosts", params=("action",), keeps_message=True)
def cb_pickHosts(ctx):
	"""Back from one host's list to the choice of host."""
	core.render_host_picker(ctx.chatId, ctx.messageId, ctx.action)

@callback(name="pickHost", params=("action", "value"), keeps_message=True)
def cb_pickHost(ctx):
	"""Steps into one host from a picker that offered several."""
	core.render_picker_for_host(ctx.chatId, ctx.messageId, ctx.action, ctx.value)


@callback(name="pickPage", params=("action", "field", "value"), keeps_message=True)
def cb_pickPage(ctx):
	"""Shows another page of a container picker too long for one keyboard."""
	try:
		page = int(ctx.value)
	except (TypeError, ValueError):
		page = 0
	core.write_cache_item(core.picker_page_key(ctx.chatId, ctx.messageId), page)
	# `field` is the host the list is about.
	core.back_to_level1_multi_aware(ctx.action, ctx.chatId, ctx.messageId, ctx.field)


# --- HOSTS ---------------------------------------------------------------

@callback(name="settingsHosts", keeps_message=True)
def cb_settingsHosts(ctx):
	core.render_settings(ctx.chatId, ctx.messageId, "hosts")


@callback(name="settingsHost", params=("value",), keeps_message=True)
def cb_settingsHost(ctx):
	"""
	One host's screen. Doubles as the "test again" button: rebuilding the
	screen re-runs the check, so there is nothing extra to wire up.
	"""
	built = core.build_settings_host(ctx.value)
	if not built:
		core.render_settings(ctx.chatId, ctx.messageId, "hosts")
		return
	text, markup = built
	core.edit_message_text(text, ctx.chatId, ctx.messageId, reply_markup=markup)


@callback(name="settingsHostAdd", keeps_message=False)
def cb_settingsHostAdd(ctx):
	core.ask_text_input(ctx.userId, "host_add", "settings_host_ask", back_to="hosts")


@callback(name="settingsHostUrl", params=("value",), keeps_message=False)
def cb_settingsHostUrl(ctx):
	core.ask_host_url(ctx.userId, ctx.value)


@callback(name="settingsHostUrlConfirm", params=("value",), keeps_message=True, answer_immediately=False)
def cb_settingsHostUrlConfirm(ctx):
	core.confirm_host_url(ctx.call.id, ctx.userId, ctx.chatId, ctx.messageId, ctx.value)


@callback(name="settingsHostUrlCancel", params=("value",), keeps_message=True, answer_immediately=False)
def cb_settingsHostUrlCancel(ctx):
	core.cancel_host_url(ctx.call.id, ctx.userId, ctx.chatId, ctx.messageId, ctx.value)


@callback(name="settingsHostRename", params=("value",), keeps_message=False)
def cb_settingsHostRename(ctx):
	core.ask_text_input(ctx.userId, f"host_rename:{ctx.value}", "settings_host_ask_name", back_to="hosts")


@callback(name="settingsHostPause", params=("value",), keeps_message=True)
def cb_settingsHostPause(ctx):
	"""
	Pauses a host, or resumes it. One button: the screen it repaints says which
	of the two it now offers, so there is no state to get out of step.

	No confirmation, unlike removing: pausing loses nothing and the same button
	undoes it.
	"""
	paused = not host_registry.is_paused(ctx.value)
	if host_registry.set_paused(ctx.value, paused):
		if paused:
			# Nothing will ask this host anything until it is resumed, so its
			# connection is closed rather than left open — behind an ssh one
			# there is a process, and a host paused for a week would hold it
			# for a week.
			core.disconnect_host(ctx.value)
		core.send_message(message=get_text(
			"settings_host_paused_done" if paused else "settings_host_resumed_done",
			core.host_alias(ctx.value)))
	built = core.build_settings_host(ctx.value)
	if not built:
		core.render_settings(ctx.chatId, ctx.messageId, "hosts")
		return
	text, markup = built
	core.edit_message_text(text, ctx.chatId, ctx.messageId, reply_markup=markup)


@callback(name="settingsHostRemove", params=("value",), keeps_message=True)
def cb_settingsHostRemove(ctx):
	built = core.build_settings_host_remove(ctx.value)
	if not built:
		core.render_settings(ctx.chatId, ctx.messageId, "hosts")
		return
	text, markup = built
	core.edit_message_text(text, ctx.chatId, ctx.messageId, reply_markup=markup)


@callback(name="settingsHostRemoveConfirm", params=("value",), keeps_message=True)
def cb_settingsHostRemoveConfirm(ctx):
	alias = core.host_alias(ctx.value)
	orphaned = core.schedule_manager.on_host(ctx.value)
	if host_registry.remove_host(ctx.value):
		# The supervisor notices on its next pass and stops that host's event
		# stream; dropping the manager keeps a stale client from being reused
		# if the same id ever comes back. Only this host's: the rest of the
		# fleet has no reason to reconnect because one of them was removed.
		core.forget_manager(ctx.value)
		core.send_message(message=get_text("settings_host_removed", alias))
		# Switched off rather than deleted: moving one to another host and
		# switching it back on is a couple of taps, while a cron and a command
		# typed long ago are gone for good.
		names = []
		for task in orphaned:
			if core.schedule_manager.update(task["id"], enabled=False):
				names.append(task["name"])
		if names:
			listed = "\n".join(f"· <b>{html.escape(name)}</b>" for name in names)
			core.send_message(message=get_text("settings_host_schedules_disabled", listed))
	core.render_settings(ctx.chatId, ctx.messageId, "hosts")


@callback(name="cerrar")
def cb_cerrar(ctx):
	"""Closes a menu, dropping everything cached against its message."""
	if core.read_cache_item(f"update_data_{ctx.chatId}_{ctx.messageId}") is not None:
		core.clear_update_data(ctx.chatId, ctx.messageId)
	core.clear_container_cache(ctx.chatId, ctx.messageId)
	core.clear_multi_action(ctx.chatId, ctx.messageId)
	core.clear_container_list(ctx.chatId, ctx.messageId)
	core.clear_info_cache(ctx.chatId, ctx.messageId)
