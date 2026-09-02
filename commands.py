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

import store

from telebot.types import InlineKeyboardButton
from telebot.types import InlineKeyboardMarkup

from config import CONTAINER_ID_LENGTH, CONTAINER_NAME, ICON_CONTAINER_MARK_FOR_UPDATE
from i18n import get_text
from core import (
	send_prune_menu,
	send_picker, manager_for, ref_id,
	register_command,
	VERSION, ask_command, ask_text_input,
	build_hierarchical_keyboard, button_columns, change_tag_container,
	compose, confirm_delete, create_simple_keyboard,
	delete_message, display_all_hosts, docker_manager,
	info, log_file, logs,
	mute, print_donors, restart,
	run, save_container_cache, save_multi_action,
	save_update_data, send_message, send_settings_menu,
	send_ports_menu, show_schedule_menu, sort_containers_by_priority,
	stop, update_available,
)



#
# One function per command, so that typing /run and pressing the Run button
# in /start go through the exact same code. They all take the same arguments
# and ignore what they do not need, which keeps the dispatch table trivial.
# `container_id` is None whenever the command was invoked without naming a
# container, which is always the case for a button press.

def cmd_list(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	send_message(message=display_all_hosts(comando="/list"),
				reply_markup=create_simple_keyboard("button_close"))

def cmd_run(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		run(container_id, container_name)
		return
	send_picker("Run")

def cmd_stop(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		stop(container_id, container_name)
		return
	send_picker("Stop")

def cmd_restart(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		restart(container_id, container_name)
		return
	send_picker("Restart")

def cmd_logs(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		logs(container_id, container_name)
		return
	send_picker("Logs")

def cmd_logfile(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		log_file(container_id, container_name)
		return
	send_picker("Logfile")

def cmd_compose(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		compose(container_id, container_name)
		return
	send_picker("Compose")

def cmd_schedule(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	show_schedule_menu(user_id, chat_id)

def cmd_settings(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	send_settings_menu()

def cmd_info(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		info(container_id, container_name)
		return
	send_picker("Info")

def cmd_exec(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		ask_command(user_id, container_id, container_name)
		return
	send_picker("Exec")

def cmd_delete(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		confirm_delete(container_id, container_name)
		return
	send_picker("Delete")

def cmd_checkupdate(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	if container_id:
		manager_for(container_id).force_check_update(ref_id(container_id))
		return
	send_picker("CheckUpdate")

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
		return
	send_picker("ChangeTag")

def cmd_prune(user_id=None, chat_id=None, container_id=None, container_name=None, argument=None):
	send_prune_menu()

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
	send_ports_menu()

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
