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
from config import CONTAINER_ID_LENGTH, SUPPORTED_LANGUAGES
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
	core.perform_container_update(ctx.containerId, ctx.containerName)

@callback(
	name='updateAll',
)
def cb_updateAll(ctx):
	"""
	Updates everything with a pending update, on every reachable host.

	Host by host in sequence, because updating pulls images and doing several
	machines at once would fight over the same network.
	"""
	for entry, owner, containers in core.hosts_with_containers():
		for container in core.sort_containers_by_priority(containers):
			if core.update_available(container, entry["id"]):
				core.perform_container_update(
					core.container_ref(entry["id"], container), container.name)

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
	command = core.load_command_cache(ctx.commandId)
	core.clear_command_cache(ctx.commandId)
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
	x = core.send_message(message=get_text("deleting", ctx.containerName))
	result = core.manager_for(ctx.containerId).delete(
		container_id=core.ref_id(ctx.containerId), container_name=ctx.containerName)
	core.delete_message(x.message_id)
	core.send_message(message=result)

@callback(
	name='changeTagContainer',
	params=('containerId',),
)
def cb_changeTagContainer(ctx):
	# Get container name from cache or Docker
	ctx.containerName = core.get_container_name(ctx.chatId, ctx.messageId, ctx.containerId)
	if not ctx.containerName:
		ctx.containerName = "Unknown"
	core.change_tag_container(ctx.containerId, ctx.containerName)

@callback(
	name='confirmChangeTag',
	params=('containerId', 'tag'),
)
def cb_confirmChangeTag(ctx):
	# Get container name from cache or Docker
	ctx.containerName = core.get_container_name(ctx.chatId, ctx.messageId, ctx.containerId)
	if not ctx.containerName:
		ctx.containerName = "Unknown"
	core.confirm_change_tag(ctx.containerId, ctx.containerName, ctx.tag)

@callback(
	name='changeTag',
	params=('containerId', 'tag'),
)
def cb_changeTag(ctx):
	# Get container name from cache or Docker
	ctx.containerName = core.get_container_name(ctx.chatId, ctx.messageId, ctx.containerId)
	if not ctx.containerName:
		ctx.containerName = "Unknown"
	core.perform_container_update(ctx.containerId, ctx.containerName, tag=ctx.tag)

@callback(
	name='deleteSchedule',
	params=('scheduleHash',),
)
def cb_deleteSchedule(ctx):
	schedules = core.schedule_manager.get_all_schedules()
	idx = core._validate_schedule_index(ctx.scheduleHash, schedules)
	if idx >= 0:
		schedule_to_delete = schedules[idx]
		core.schedule_manager.delete_schedule(schedule_to_delete["name"])
		core.send_message(message=get_text("deleted_schedule", schedule_to_delete["name"]))
	else:
		core.send_message(message=get_text("error_schedule_not_found"))

@callback(
	name='toggleUpdate',
	params=('containerId',),
	keeps_message=True,
	answer_immediately=False,
)
def cb_toggleUpdate(ctx):
	containers, selected = core.load_update_data(ctx.chatId, ctx.messageId)
	was_selected = ctx.containerId in selected

	if was_selected:
		selected.remove(ctx.containerId)
	else:
		selected.add(ctx.containerId)
	core.save_update_data(ctx.chatId, ctx.messageId, containers, selected)

	markup = core.build_generic_keyboard(containers, selected, ctx.messageId, "Update", get_text("button_update"), get_text("button_update_all"))

	# Use synchronous edit for immediate feedback
	try:
		core.edit_message_reply_markup_sync(ctx.chatId, ctx.messageId, reply_markup=markup)
		# Answer callback without text (no annoying popup)
		core.answer_callback_quietly(ctx.call.id)
	except Exception as e:
		core.error(f"Error updating toggle: {e}")
		core.answer_callback_quietly(ctx.call.id)

@callback(
	name='toggleUpdateAll',
	keeps_message=True,
	answer_immediately=False,
)
def cb_toggleUpdateAll(ctx):
	containers, selected = core.load_update_data(ctx.chatId, ctx.messageId)
	newly_selected_count = 0
	for cid, _cname in containers:
		if cid not in selected:
			selected.add(cid)
			newly_selected_count += 1
	core.save_update_data(ctx.chatId, ctx.messageId, containers, selected)

	markup = core.build_generic_keyboard(containers, selected, ctx.messageId, "Update", get_text("button_update"), get_text("button_update_all"))

	# Use synchronous edit for immediate feedback
	try:
		core.edit_message_reply_markup_sync(ctx.chatId, ctx.messageId, reply_markup=markup)
		# Answer callback without text (no annoying popup)
		core.answer_callback_quietly(ctx.call.id)
	except Exception as e:
		core.error(f"Error updating toggle all: {e}")
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
	containers, selected = core.load_update_data(ctx.chatId, ctx.originalMessageId)
	for ref in selected:
		# Each selection carries its own host: an /updateall list can span
		# machines, so they cannot all be looked up on the local one.
		owner, container = core.find_container(ref)
		if container is None:
			core.send_message(message=get_text("container_does_not_exist", core.ref_id(ref)))
			core.debug(f"Container {ref} not found")
			continue
		if core.update_available(container, owner.host_id):
			core.perform_container_update(core.container_ref(owner.host_id, container), container.name)
	core.clear_update_data(ctx.chatId, ctx.originalMessageId)

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
	project_info = core.manager(ctx.hostId).get_project_info(project_name)

	if not project_info:
		core.send_message(message=get_text("error_project_not_found", project_name))
		return

	# Build Level 2 keyboard
	markup = InlineKeyboardMarkup(row_width=core.button_columns())
	botones = []

	# Add individual container buttons (sorted by status and service name)
	for service_name in core.sort_project_services(project_info):
		container = project_info.services[service_name]
		status_emoji = core.get_status_emoji(container.status, container.name, container)
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
		get_text("select_container_from_project", project_name),
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
	project_info = core.manager(ctx.hostId).get_project_info(project_name)

	if not project_info:
		core.send_message(message=get_text("error_project_not_found", project_name))
		return

	container_count = project_info.get_container_count()
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
		get_text("confirm_delete_project", project_name, container_count),
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

	result, data = getattr(owner, f"prune_{kind.lower()}")()
	markup = core.create_simple_keyboard("button_delete")
	fichero_temporal = core.get_temporal_file(data, get_text(labels[kind]))
	x = core.send_message(message=get_text("loading_file"))
	core.send_document(document=fichero_temporal, reply_markup=markup, caption=result)
	if x:
		core.delete_message(x.message_id)


@callback(
	name='generatePort',
)
def cb_generatePort(ctx):
	# Generate a random available port
	port = core.get_random_available_port()

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
)
def cb_checkPort(ctx):
	# Ask user for port to check
	core.ask_port_to_check(ctx.userId)

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
		core.render_settings(ctx.chatId, ctx.messageId, core.SETTINGS_TOGGLE_SCREEN.get(ctx.field, "main"))
	else:
		core.warning(f"Ignored toggle of unknown setting: {ctx.field}")

@callback(
	name='settingsUpdates',
	keeps_message=True,
)
def cb_settingsUpdates(ctx):
	core.render_settings(ctx.chatId, ctx.messageId, "updates")

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
		ctx.action(user_id=ctx.userId, chat_id=ctx.chatId)

@callback(
	name='scheduleAdd',
)
def cb_scheduleAdd(ctx):
	core.ask_schedule_name(ctx.userId)

@callback(
	name='scheduleEdit',
)
def cb_scheduleEdit(ctx):
	core.show_schedule_edit_list(ctx.userId, ctx.chatId)

@callback(
	name='scheduleSelectEdit',
	params=('action',),
)
def cb_scheduleSelectEdit(ctx):
	schedules = core.schedule_manager.get_all_schedules()
	idx = core._validate_schedule_index(ctx.action, schedules)
	if idx >= 0:
		core.show_schedule_edit_options(ctx.userId, schedules[idx]["name"])
	else:
		core.send_message(message=get_text("error_invalid_selection"))

@callback(
	name='scheduleDelete',
)
def cb_scheduleDelete(ctx):
	core.show_schedule_delete_list(ctx.userId, ctx.chatId)

@callback(
	name='scheduleSelectDelete',
	params=('scheduleHash',),
)
def cb_scheduleSelectDelete(ctx):
	schedules = core.schedule_manager.get_all_schedules()
	idx = core._validate_schedule_index(ctx.scheduleHash, schedules)
	if idx >= 0:
		schedule_to_delete = schedules[idx]
		core.schedule_manager.delete_schedule(schedule_to_delete["name"])
		core.send_message(message=get_text("schedule_deleted", schedule_to_delete["name"]))
		# Show the updated schedule menu
		core.show_schedule_menu(ctx.userId, ctx.chatId)
	else:
		core.send_message(message=get_text("error_invalid_selection"))

@callback(
	name='scheduleSelectToggle',
	params=('scheduleHash',),
)
def cb_scheduleSelectToggle(ctx):
	schedules = core.schedule_manager.get_all_schedules()
	idx = core._validate_schedule_index(ctx.scheduleHash, schedules)
	if idx >= 0:
		schedule_to_toggle = schedules[idx]
		new_status = core.schedule_manager.toggle_schedule(schedule_to_toggle["name"])
		if new_status is not None:
			if new_status:
				core.send_message(message=get_text("schedule_enabled", schedule_to_toggle["name"]))
			else:
				core.send_message(message=get_text("schedule_disabled", schedule_to_toggle["name"]))
		else:
			core.send_message(message=get_text("error_invalid_selection"))
	else:
		core.send_message(message=get_text("error_invalid_selection"))

@callback(
	name='scheduleSelectAction',
	params=('action',),
)
def cb_scheduleSelectAction(ctx):
	schedule_state = core.load_schedule_state(ctx.userId)
	if schedule_state:
		schedule_state["action"] = ctx.action

		# Delete previous message if exists
		if schedule_state.get("last_message_id"):
			try:
				core.delete_message(schedule_state.get("last_message_id"))
			except:
				pass

		if ctx.action == "mute":
			schedule_state["step"] = "ask_minutes"
			schedule_state["show_output"] = None  # Not applicable for mute

			# Build message with summary
			message_text = core._build_schedule_summary(schedule_state)
			message_text += f"\n\n{get_text('schedule_ask_minutes')}"

			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			schedule_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, schedule_state)
		elif ctx.action == "prune":
			schedule_state["container"] = None  # Not applicable for prune
			core.ask_schedule_prune_type(ctx.userId, schedule_state)
		else:
			# For run, stop, restart, exec - ask for container
			# show_output will remain None until after container selection for exec
			schedule_state["show_output"] = None
			core.save_schedule_state(ctx.userId, schedule_state)
			core.show_schedule_container_selection(ctx.userId, ctx.action)

@callback(
	name='scheduleSelectContainer',
	params=('containerIdx',),
)
def cb_scheduleSelectContainer(ctx):
	schedule_state = core.load_schedule_state(ctx.userId)
	if schedule_state:
		# Retrieve container name from state mapping (containerIdx is the index)
		container_key = f"container_{ctx.containerIdx}"
		container_name = schedule_state.get(container_key)
		if container_name:
			schedule_state["container"] = container_name
			# The host travels with the name: a task names a container, and a
			# name is only unique within one daemon.
			schedule_state["host"] = schedule_state.get(f"container_host_{ctx.containerIdx}")
		else:
			core.error(f"Container not found in state for key: {container_key}")
			core.send_message(message=get_text("error_invalid_selection"))
			return

		# Delete previous message if exists
		if schedule_state.get("last_message_id"):
			try:
				core.delete_message(schedule_state.get("last_message_id"))
			except:
				pass

		# If action is exec, ask for show_output; otherwise confirm
		if schedule_state.get("action") == "exec":
			schedule_state["step"] = "ask_show_output"
			schedule_state["show_output"] = False  # Initialize for display

			# Build message with summary
			message_text = f"<b>{get_text('schedule_label_name')}:</b> {schedule_state.get('name')}\n"
			message_text += f"<b>{get_text('schedule_label_cron')}:</b> {schedule_state.get('cron')}\n"
			message_text += f"<b>{get_text('schedule_label_action')}:</b> {schedule_state.get('action')}\n"
			message_text += f"<b>{get_text('schedule_label_container')}:</b> {container_name}\n\n"
			message_text += get_text("schedule_ask_show_output")

			markup = InlineKeyboardMarkup(row_width=2)
			markup.add(
				InlineKeyboardButton(get_text("button_yes"), callback_data="scheduleSelectShowOutput|yes"),
				InlineKeyboardButton(get_text("button_no"), callback_data="scheduleSelectShowOutput|no")
			)
			msg = core.send_message(message=message_text, reply_markup=markup)
			schedule_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, schedule_state)
		else:
			schedule_state["step"] = "confirm"
			core.save_schedule_state(ctx.userId, schedule_state)
			core.confirm_schedule_creation(ctx.userId, schedule_state)

@callback(
	name='scheduleSelectShowOutput',
	params=('action',),
)
def cb_scheduleSelectShowOutput(ctx):
	schedule_state = core.load_schedule_state(ctx.userId)
	if schedule_state:
		schedule_state["show_output"] = (ctx.action == "yes")
		schedule_state["step"] = "ask_command"

		# Delete previous message if exists
		if schedule_state.get("last_message_id"):
			try:
				core.delete_message(schedule_state.get("last_message_id"))
			except:
				pass

		# Build message with summary
		message_text = core._build_schedule_summary(schedule_state)
		message_text += f"\n\n{get_text('schedule_ask_command')}"

		markup = InlineKeyboardMarkup(row_width=1)
		markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
		msg = core.send_message(message=message_text, reply_markup=markup)
		schedule_state["last_message_id"] = msg.message_id if msg else None
		core.save_schedule_state(ctx.userId, schedule_state)

@callback(name="scheduleSelectHost", params=("value",))
def cb_scheduleSelectHost(ctx):
	"""Records which host a scheduled prune will clean, then asks what to clean."""
	schedule_state = core.load_schedule_state(ctx.userId)
	if not schedule_state:
		return
	if core.host_registry.host(ctx.value) is None:
		core.send_message(message=get_text("error_invalid_selection"))
		return
	schedule_state["host"] = ctx.value
	core.ask_schedule_prune_show_output(ctx.userId, schedule_state)


@callback(
	name='scheduleSelectPruneType',
	params=('pruneType',),
)
def cb_scheduleSelectPruneType(ctx):
	schedule_state = core.load_schedule_state(ctx.userId)
	if schedule_state:
		schedule_state["prune_type"] = ctx.pruneType
		# With one host there is nothing to ask, so the flow is unchanged.
		if core.host_registry.is_single_host():
			core.ask_schedule_prune_show_output(ctx.userId, schedule_state)
		else:
			core.ask_schedule_prune_host(ctx.userId, schedule_state)

@callback(
	name='scheduleSelectPruneShowOutput',
	params=('action',),
)
def cb_scheduleSelectPruneShowOutput(ctx):
	schedule_state = core.load_schedule_state(ctx.userId)
	if schedule_state:
		schedule_state["show_output"] = (ctx.action == "yes")
		schedule_state["step"] = "confirm"

		# Delete previous message if exists
		if schedule_state.get("last_message_id"):
			try:
				core.delete_message(schedule_state.get("last_message_id"))
			except:
				pass

		core.save_schedule_state(ctx.userId, schedule_state)
		core.confirm_schedule_creation(ctx.userId, schedule_state)

@callback(
	name='scheduleConfirm',
)
def cb_scheduleConfirm(ctx):
	schedule_state = core.load_schedule_state(ctx.userId)
	if schedule_state:
		try:
			core.schedule_manager.add_schedule(
				name=schedule_state["name"],
				cron=schedule_state["cron"],
				action=schedule_state["action"],
				container=schedule_state.get("container"),
				minutes=schedule_state.get("minutes"),
				show_output=schedule_state.get("show_output", False),
				command=schedule_state.get("command"),
				prune_type=schedule_state.get("prune_type"),
				host=schedule_state.get("host")
			)
			core.send_message(message=get_text("schedule_added_success", schedule_state["name"]))
			core.clear_schedule_state(ctx.userId)
			# Show the updated schedule menu
			core.show_schedule_menu(ctx.userId, ctx.chatId)
		except Exception as e:
			core.send_message(message=get_text("error_adding_schedule", str(e)))
			core.error(f"Error adding schedule: {e}")

@callback(
	name='scheduleEditField',
	params=('field', 'scheduleId'),
)
def cb_scheduleEditField(ctx):
	if ctx.field and ctx.scheduleId:
		schedule = core.schedule_manager.get_schedule_by_id(int(ctx.scheduleId))
		if not schedule:
			core.send_message(message=get_text("error_invalid_selection"))
			return

		schedule_name = schedule.get('name', '')

		# Initialize edit state
		edit_state = {
			"schedule_name": schedule_name,
			"schedule_id": int(ctx.scheduleId),
			"field": ctx.field,
			"last_message_id": None
		}

		# Ask for the new value based on field
		if ctx.field == "name":
			message_text = f"<b>{get_text('schedule_edit_name')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_name')}\n"
			message_text += f"<i>{get_text('current_value')}: {schedule_name}</i>"

			# For text fields, ask for input
			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

		elif ctx.field == "cron":
			current_cron = schedule.get('cron', '* * * * *')
			message_text = f"<b>{get_text('schedule_edit_cron')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_cron')}\n"
			message_text += f"<i>{get_text('current_value')}: {current_cron}</i>"

			# For text fields, ask for input
			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

		elif ctx.field == "container":
			current_container = schedule.get('container', '')
			message_text = f"<b>{get_text('schedule_edit_container')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_container')}\n"
			message_text += f"<i>{get_text('current_value')}: {current_container}</i>\n\n"

			# Show container selection
			available_containers = core._get_available_containers()

			if not available_containers:
				core.send_message(message=get_text("error_no_containers_available"))
				return

			markup = InlineKeyboardMarkup(row_width=2)
			# Store container mapping to avoid callback length issues (64 char limit)
			for idx, container in enumerate(available_containers):
				markup.add(InlineKeyboardButton(container.name, callback_data=f"scheduleEditValue|container|{ctx.scheduleId}|{idx}"))
				edit_state[f"container_{idx}"] = container.name
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

		elif ctx.field == "minutes":
			current_minutes = schedule.get('minutes', '')
			message_text = f"<b>{get_text('schedule_edit_minutes')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_minutes')}\n"
			message_text += f"<i>{get_text('current_value')}: {current_minutes}</i>"

			# For text fields, ask for input
			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

		elif ctx.field == "command":
			current_command = schedule.get('command', '')
			message_text = f"<b>{get_text('schedule_edit_command')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_command')}\n"
			message_text += f"<i>{get_text('current_value')}: {current_command}</i>"

			# For text fields, ask for input
			markup = InlineKeyboardMarkup(row_width=1)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

		elif ctx.field == "show_output":
			current_output = schedule.get('show_output', False)
			message_text = f"<b>{get_text('schedule_edit_show_output')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_show_output')}\n"
			message_text += f"<i>{get_text('current_value')}: {get_text('schedule_yes') if current_output else get_text('schedule_no')}</i>"

			markup = InlineKeyboardMarkup(row_width=2)
			markup.add(
				InlineKeyboardButton(get_text("button_yes"), callback_data=f"scheduleEditValue|show_output|{ctx.scheduleId}|yes"),
				InlineKeyboardButton(get_text("button_no"), callback_data=f"scheduleEditValue|show_output|{ctx.scheduleId}|no")
			)
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

		elif ctx.field == "prune_type":
			current_prune_type = schedule.get('prune_type', '')
			message_text = f"<b>{get_text('schedule_edit_prune_type')}</b>\n\n"
			message_text += f"{get_text('schedule_ask_prune_type')}\n"
			message_text += f"<i>{get_text('current_value')}: {current_prune_type}</i>"

			markup = InlineKeyboardMarkup(row_width=2)
			markup.add(
				InlineKeyboardButton(get_text("schedule_prune_containers"), callback_data=f"scheduleEditValue|prune_type|{ctx.scheduleId}|containers"),
				InlineKeyboardButton(get_text("schedule_prune_images"), callback_data=f"scheduleEditValue|prune_type|{ctx.scheduleId}|images"),
				InlineKeyboardButton(get_text("schedule_prune_networks"), callback_data=f"scheduleEditValue|prune_type|{ctx.scheduleId}|networks"),
				InlineKeyboardButton(get_text("schedule_prune_volumes"), callback_data=f"scheduleEditValue|prune_type|{ctx.scheduleId}|volumes")
			)
			markup.add(InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar"))
			msg = core.send_message(message=message_text, reply_markup=markup)
			edit_state["last_message_id"] = msg.message_id if msg else None
			core.save_schedule_state(ctx.userId, edit_state)

@callback(
	name='scheduleEditValue',
	params=('field', 'scheduleId', 'value'),
)
def cb_scheduleEditValue(ctx):
	if ctx.field and ctx.scheduleId and ctx.value:
		schedule = core.schedule_manager.get_schedule_by_id(int(ctx.scheduleId))
		if not schedule:
			core.send_message(message=get_text("error_invalid_selection"))
			return

		schedule_name = schedule.get('name', '')

		# Update the schedule based on field type
		if ctx.field == "show_output":
			core.schedule_manager.update_schedule(schedule_name, show_output=(ctx.value == "yes"))
			core.send_message(message=get_text("schedule_updated_success", schedule_name))
		elif ctx.field == "prune_type":
			core.schedule_manager.update_schedule(schedule_name, prune_type=ctx.value)
			core.send_message(message=get_text("schedule_updated_success", schedule_name))
		elif ctx.field == "container":
			# value is now the container index, retrieve name from edit state
			edit_state = core.load_schedule_state(ctx.userId)
			container_name = edit_state.get(f"container_{ctx.value}") if edit_state else None

			if container_name:
				core.schedule_manager.update_schedule(schedule_name, container=container_name)
				core.send_message(message=get_text("schedule_updated_success", schedule_name))
			else:
				core.send_message(message=get_text("error_invalid_selection"))
				return
		elif ctx.field == "command":
			core.schedule_manager.update_schedule(schedule_name, command=ctx.value)
			core.send_message(message=get_text("schedule_updated_success", schedule_name))

		# Show the schedule menu again
		core.show_schedule_menu(ctx.userId, ctx.chatId)

@callback(
	name='scheduleEditStatus',
	params=('scheduleId',),
)
def cb_scheduleEditStatus(ctx):
	if ctx.scheduleId:
		schedule = core.schedule_manager.get_schedule_by_id(int(ctx.scheduleId))

		if schedule:
			schedule_name = schedule.get('name', '')

			# Toggle the status
			new_enabled = not schedule.get("enabled", True)
			core.schedule_manager.update_schedule(schedule_name, enabled=new_enabled)

			# Show success message
			core.send_message(message=get_text("schedule_updated_success", schedule_name))

			# Show the schedule menu again
			core.show_schedule_menu(ctx.userId, ctx.chatId)
		else:
			core.send_message(message=get_text("error_invalid_selection"))
	else:
		core.send_message(message=get_text("error_invalid_selection"))

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
		if multi:
			core.back_to_level1_multi_aware(action, ctx.chatId, ctx.messageId, ctx.hostId)
			return
		result = core.build_back_to_level1_keyboard(action, ctx.chatId, ctx.messageId, host_id=ctx.hostId)
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
	result = core.build_back_to_level1_keyboard("Compose", ctx.chatId, ctx.messageId, host_id=ctx.hostId)
	if result:
		markup, message_key = result
		core.edit_message_text(get_text(message_key), ctx.chatId, ctx.messageId, reply_markup=markup)


register_callback("backToComposeLevel1", _back_to_compose_level1, keeps_message=True)


@callback(name="pickHost", params=("action", "value"), keeps_message=True)
def cb_pickHost(ctx):
	"""Steps into one host from a picker that offered several."""
	core.render_picker_for_host(ctx.chatId, ctx.messageId, ctx.action, ctx.value)


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


@callback(name="settingsHostRename", params=("value",), keeps_message=False)
def cb_settingsHostRename(ctx):
	core.ask_text_input(ctx.userId, f"host_rename:{ctx.value}", "settings_host_ask_name", back_to="hosts")


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
	if host_registry.remove_host(ctx.value):
		# The supervisor notices on its next pass and stops that host's event
		# stream; dropping the manager keeps a stale client from being reused
		# if the same id ever comes back.
		core.forget_managers()
		core.send_message(message=get_text("settings_host_removed", alias))
	core.render_settings(ctx.chatId, ctx.messageId, "hosts")


@callback(name="cerrar")
def cb_cerrar(ctx):
	"""Closes a menu, dropping everything cached against its message."""
	if core.read_cache_item(f"update_data_{ctx.chatId}_{ctx.messageId}") is not None:
		core.clear_update_data(ctx.chatId, ctx.messageId)
	core.clear_container_cache(ctx.chatId, ctx.messageId)
	core.clear_multi_action(ctx.chatId, ctx.messageId)
