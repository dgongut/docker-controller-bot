"""
The commands the bot answers.

One function per command, so that typing /run and pressing the Run button in
/start go through the exact same code. They all take the same arguments and
ignore what they do not need, which keeps the dispatch trivial.

`container_id` is None whenever the command was invoked without naming a
container, which is always the case for a button press.

Importing this module is what registers the commands, so the entry point has
to import it before polling starts.
"""

import time

from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup

from config import CONTAINER_ID_LENGTH, CONTAINER_NAME
from i18n import get_text
from core import (
	register_command,
	VERSION, ask_command, ask_text_input,
	build_hierarchical_keyboard, button_columns, change_tag_container,
	compose, confirm_delete, create_simple_keyboard,
	delete_message, display_containers, docker_manager,
	info, log_file, logs,
	mute, print_donors, restart,
	run, save_container_cache, save_multi_action,
	save_update_data, send_message, send_settings_menu,
	show_container_ports, show_schedule_menu, sort_containers_by_priority,
	stop, update_available,
)



#
# One function per command, so that typing /run and pressing the Run button
# in /start go through the exact same code. They all take the same arguments
# and ignore what they do not need, which keeps the dispatch table trivial.
# `container_id` is None whenever the command was invoked without naming a
# container, which is always the case for a button press.

def cmd_list(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	containers = docker_manager.list_containers(comando="/list")
	send_message(message=display_containers(containers), reply_markup=create_simple_keyboard("button_close"))

def cmd_run(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		run(container_id, container_name)
	else:
		# Get ALL containers to show projects with all containers, but filter standalone to only stopped
		containers = docker_manager.list_containers()
		if not containers or all(c.name == CONTAINER_NAME for c in containers):
			send_message(message=get_text("no_containers_to_start"))
			return

		# Use hierarchical keyboard with filters:
		# - Standalone: only stopped/paused/exited/created
		# - Projects: hide if ALL containers are running/restarting
		markup, standalone_containers = build_hierarchical_keyboard(
			containers, "Run", CONTAINER_NAME,
			filter_standalone_status=['exited', 'stopped', 'paused', 'created'],
			filter_projects_with_all_status=['running', 'restarting']
		)
		sent_message = send_message(message=get_text("start_a_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
		# Keep this menu open so several containers can be picked in a row
		if sent_message and store.get("bot.multi_selection"):
			save_multi_action(sent_message.chat.id, sent_message.message_id, "Run")

def cmd_stop(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		stop(container_id, container_name)
	else:
		# Get ALL containers to show projects with all containers, but filter standalone to only running
		containers = docker_manager.list_containers()
		if not containers or all(c.name == CONTAINER_NAME for c in containers):
			send_message(message=get_text("no_containers_to_stop"))
			return

		# Use hierarchical keyboard with filters:
		# - Standalone: only running/restarting
		# - Projects: hide if ALL containers are stopped/paused/exited/created
		markup, standalone_containers = build_hierarchical_keyboard(
			containers, "Stop", CONTAINER_NAME,
			filter_standalone_status=['running', 'restarting'],
			filter_projects_with_all_status=['exited', 'stopped', 'paused', 'created']
		)
		sent_message = send_message(message=get_text("stop_a_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
		# Keep this menu open so several containers can be picked in a row
		if sent_message and store.get("bot.multi_selection"):
			save_multi_action(sent_message.chat.id, sent_message.message_id, "Stop")

def cmd_restart(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		restart(container_id, container_name)
	else:
		# Get ALL containers (not just running) to show with status indicators
		containers = docker_manager.list_containers()
		if not containers or all(c.name == CONTAINER_NAME for c in containers):
			send_message(message=get_text("no_containers_to_restart"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		markup, standalone_containers = build_hierarchical_keyboard(containers, "Restart", CONTAINER_NAME)
		sent_message = send_message(message=get_text("restart_a_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)
		# Keep this menu open so several containers can be picked in a row
		if sent_message and store.get("bot.multi_selection"):
			save_multi_action(sent_message.chat.id, sent_message.message_id, "Restart")

def cmd_logs(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		logs(container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("no_containers_for_logs"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for logs (can't get logs from whole project)
		# Filter: show all containers (you can see logs from any container)
		# Don't exclude bot container for logs (we want to see bot logs too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"Logs",
			None  # Don't exclude any container
		)
		sent_message = send_message(message=get_text("logs_command_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_logfile(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		log_file(container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("no_containers_for_logs"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for logfile (can't get logfile from whole project)
		# Filter: show all containers (you can get logfile from any container)
		# Don't exclude bot container for logfile (we want to see bot logfile too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"Logfile",
			None  # Don't exclude any container
		)
		sent_message = send_message(message=get_text("show_logsfile"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_compose(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		compose(container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("error_no_containers_available"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for compose (can't get compose file from whole project)
		# Filter: show all containers (you can get compose file from any container)
		# Don't exclude bot container for compose (we want to see bot compose too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"Compose",
			None  # Don't exclude any container
		)
		sent_message = send_message(message=get_text("show_compose"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_schedule(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	show_schedule_menu(user_id, chat_id)

def cmd_settings(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	send_settings_menu()

def cmd_info(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		info(container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("no_containers_for_info"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for info (can't get info from whole project)
		# Filter: show all containers (you can see info from any container)
		# Don't exclude bot container (we want to see bot info too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"Info",
			None  # Don't exclude any container
		)
		sent_message = send_message(message=get_text("info_command_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_exec(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		ask_command(user_id, container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("no_containers_for_exec"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for exec (can't exec on whole project)
		# Filter: only show running/restarting containers and projects with at least one running container
		# Don't exclude bot container (we want to exec into the bot too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"Exec",
			None,  # Don't exclude any container
			filter_standalone_status=['running', 'restarting'],
			filter_projects_with_all_status=['exited', 'paused', 'dead', 'created']
		)
		sent_message = send_message(message=get_text("exec_command_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_delete(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		confirm_delete(container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers or all(c.name == CONTAINER_NAME for c in containers):
			send_message(message=get_text("no_containers_to_delete"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		markup, standalone_containers = build_hierarchical_keyboard(containers, "Delete", CONTAINER_NAME)
		sent_message = send_message(message=get_text("delete_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_checkupdate(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		docker_manager.force_check_update(container_id)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("no_containers_for_checkupdate"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for checkupdate (can't check updates on whole project)
		# Filter: show all containers (you can check updates on any container)
		# Don't exclude bot container (we want to check bot updates too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"CheckUpdate",
			None  # Don't exclude any container
		)
		sent_message = send_message(message=get_text("checkupdate_command_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_updateall(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	containers = docker_manager.list_containers()
	# Sort containers: bot first, then running, then stopped (all alphabetically)
	sorted_containers = sort_containers_by_priority(containers)
	containersToUpdate = []  # list of [id, name] pairs
	containersToUpdateObjs = []
	for container in sorted_containers:
		if update_available(container):
			containersToUpdate.append([container.id[:CONTAINER_ID_LENGTH], container.name])
			containersToUpdateObjs.append(container)
	if not containersToUpdate:
		send_message(message=get_text("already_updated_all"))
		return

	markup = InlineKeyboardMarkup(row_width=button_columns())
	markup.add(*[
		InlineKeyboardButton(f'{ICON_CONTAINER_MARK_FOR_UPDATE} {cname}', callback_data=f'toggleUpdate|{cid}')
		for cid, cname in containersToUpdate
	])
	markup.add(
		InlineKeyboardButton(get_text("button_update_all"), callback_data="toggleUpdateAll"),
		InlineKeyboardButton(get_text("button_cancel"), callback_data="cerrar")
	)
	message = send_message(message=get_text("available_updates", len(containersToUpdate)), reply_markup=markup)
	if message:
		save_update_data(message.chat.id, message.message_id, containersToUpdate)
		# Pre-populate name cache so callback parser can resolve names from IDs
		save_container_cache(message.chat.id, message.message_id, containersToUpdateObjs)

def cmd_changetag(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		change_tag_container(container_id, container_name)
	else:
		# Get ALL containers to show projects and standalone
		containers = docker_manager.list_containers()
		if not containers:
			send_message(message=get_text("error_no_containers_available"))
			return

		# Use hierarchical keyboard (Level 1: projects + standalone containers)
		# No project-level action for changetag (can't change tag for whole project)
		# Filter: show all containers (you can change tag on any container)
		# Don't exclude bot container (we want to change bot tag too)
		markup, standalone_containers = build_hierarchical_keyboard(
			containers,
			"ChangeTag",
			None  # Don't exclude any container
		)
		sent_message = send_message(message=get_text("change_tag_container"), reply_markup=markup)
		# Save container cache for standalone containers
		if sent_message and standalone_containers:
			save_container_cache(sent_message.chat.id, sent_message.message_id, standalone_containers)

def cmd_prune(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	markup = InlineKeyboardMarkup(row_width=button_columns())
	botones = []
	botones.append(InlineKeyboardButton(get_text("button_containers"), callback_data=f'prune|confirmPruneContainers'))
	botones.append(InlineKeyboardButton(get_text("button_images"), callback_data=f'prune|confirmPruneImages'))
	botones.append(InlineKeyboardButton(get_text("button_networks"), callback_data=f'prune|confirmPruneNetworks'))
	botones.append(InlineKeyboardButton(get_text("button_volumes"), callback_data=f'prune|confirmPruneVolumes'))
	markup.add(*botones)
	markup.add(InlineKeyboardButton(get_text("button_close"), callback_data="cerrar"))
	send_message(message=get_text("prune_system"), reply_markup=markup)

def cmd_version(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	x = send_message(message=get_text("version", VERSION))
	if x:
		time.sleep(15)
		delete_message(x.message_id)

def cmd_donate(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	x = send_message(message=get_text("donate"))
	if x:
		time.sleep(45)
		delete_message(x.message_id)

def cmd_donors(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	print_donors()

def cmd_ports(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	show_container_ports()

def cmd_mute(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	"""
	Silences notifications for a number of minutes.

	Typed as `/mute 30` the argument is there; pressed as a button there is no
	way to carry one, so it asks rather than erroring out.
	"""
	if argument is None:
		ask_text_input(user_id, "mute_minutes", "mute_ask_minutes", back_to=None)
		return
	try:
		minutes = int(argument)
	except (TypeError, ValueError):
		send_message(message=get_text("error_use_mute_command"))
		return
	mute(minutes)

# Registered by importing this module. The core reads the registry rather than
# importing this file, which is what keeps the dependency going one way only.
for _name, _action in {
	"/list": cmd_list,
	"/run": cmd_run,
	"/stop": cmd_stop,
	"/restart": cmd_restart,
	"/delete": cmd_delete,
	"/exec": cmd_exec,
	"/logs": cmd_logs,
	"/logfile": cmd_logfile,
	"/info": cmd_info,
	"/checkupdate": cmd_checkupdate,
	"/updateall": cmd_updateall,
	"/changetag": cmd_changetag,
	"/prune": cmd_prune,
	"/ports": cmd_ports,
	"/compose": cmd_compose,
	"/schedule": cmd_schedule,
	"/mute": cmd_mute,
	"/settings": cmd_settings,
	"/version": cmd_version,
	"/donate": cmd_donate,
	"/donors": cmd_donors,
}.items():
	register_command(_name, _action)
