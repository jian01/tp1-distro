import base64
import logging
import os
from collections import deque
from datetime import datetime, timezone
from multiprocessing import Pipe, Process
from typing import NoReturn, NamedTuple, Optional

from backup_utils.backup_file import BackupFile
from src.backup_scheduler.client_request_handler import ClientRequestHandler
from src.backup_scheduler.node_handler_process import NodeHandlerProcess, CORRECT_FILE_FORMAT, WIP_FILE_FORMAT, \
    SAME_FILE_FORMAT
from src.database.database import Database
from src.database.entities.finished_task import FinishedTask

SECONDS_TO_MINUTES = 60
WRITE_FILE_PATH_TEMPLATE = '%s/backup_%d_%s_%s'
SECONDS_TO_WAIT_CLIENT = 10
MAX_FINISHED_TASKS_TO_STORE = 10


class ScheduledTask(NamedTuple):
    node_name: str
    node_address: str
    node_port: int
    node_path: str
    frequency: int
    last_checksum: str
    last_backup: Optional[datetime] = None

    def should_run(self) -> bool:
        """
        Calculates whether it should run or not

        :return: a boolean
        """
        return not self.last_backup or \
               (datetime.now() - self.last_backup).seconds / SECONDS_TO_MINUTES > self.frequency


class RunningTask(NamedTuple):
    write_file_path: str
    process: Process

    def is_running(self):
        return self.process.is_alive()

    def backup_is_correct(self) -> bool:
        """
        Cleanup all backup unnecessary files, return True if backup is of, else False

        :return: a boolean
        """
        if os.path.isfile(CORRECT_FILE_FORMAT % self.write_file_path):
            os.remove(CORRECT_FILE_FORMAT % self.write_file_path)
            return True
        else:
            if os.path.isfile(self.write_file_path):
                os.remove(self.write_file_path)
            if os.path.isfile(WIP_FILE_FORMAT % self.write_file_path):
                os.remove(WIP_FILE_FORMAT % self.write_file_path)
            return False

    def backup_is_same(self) -> bool:
        """
        Checks if the backup is the same as the previous one

        :return: a boolean
        """
        if os.path.isfile(SAME_FILE_FORMAT % self.write_file_path):
            os.remove(SAME_FILE_FORMAT % self.write_file_path)
            return True
        else:
            return False


class BackupScheduler:
    """
    Backup scheduler
    """
    logger = logging.getLogger(__module__)

    def _reload_schedule(self):
        BackupScheduler.logger.debug("Reloading schedule for backup")
        self.schedule = []
        for node_name in self.database.get_node_names():
            node_address, node_port = self.database.get_node_address(node_name)
            for path, frequency in self.database.get_tasks_for_node(node_name):
                finished_tasks = self.database.get_node_finished_tasks(node_name, path)
                last_backup = (finished_tasks[0].timestamp if finished_tasks else None)
                last_checksum = (finished_tasks[0].checksum if finished_tasks else "")
                self.schedule.append(ScheduledTask(node_name=node_name, node_address=node_address,
                                                   node_port=node_port, node_path=path,
                                                   frequency=frequency,
                                                   last_backup=last_backup,
                                                   last_checksum=last_checksum))

    def _clean_backup_path(self) -> NoReturn:
        """
        Cleans all files that are not part of registered backups
        """
        valid_file_prefixes = set()
        node_names = self.database.get_node_names()
        for node_name in node_names:
            for node_path, _ in self.database.get_tasks_for_node(node_name):
                for ft in self.database.get_node_finished_tasks(node_name, node_path)[:MAX_FINISHED_TASKS_TO_STORE]:
                    valid_file_prefixes.update([ft.result_path])
        valid_file_prefixes.update([task.write_file_path for task in self.running_tasks.values()])
        files_in_directory = os.listdir(self.backup_path)
        files_to_delete = [f for f in files_in_directory if
                           self.backup_path + "/" + f.split(".")[0] not in valid_file_prefixes]
        for file_to_delete in files_to_delete:
            os.remove(self.backup_path + "/" + file_to_delete)

    def __init__(self, backup_path: str, database: Database,
                 pipe_request_read: Pipe, pipe_request_answer: Pipe,
                 max_processes_for_tasks: int):
        """
        Initializes the backup scheduler

        :param backup_path: the path where to make the backups
        :param database: the database to use
        :param pipe_request_read: the read end pipe to handle controller commands
        :param pipe_request_answer: the read end pipe to handle controller commands
        :param max_processes_for_tasks: the maximum number of processes for tasks
        """
        self.backup_path = backup_path
        self.database = database
        self.pipe_request_read = pipe_request_read
        self.pipe_request_answer = pipe_request_answer
        self.schedule = []
        self.client_controller_process = None
        self.running_tasks = {}
        self.command_parser = ClientRequestHandler(database)
        self.task_queue = deque()
        self.max_processes = max_processes_for_tasks

    @staticmethod
    def safe_base64(text: str) -> str:
        """
        Generates a safe base64 for filenames according rfc3548
        :param text: the text to encode
        :return: the safe text
        """
        return base64.b64encode(bytes(text, 'ascii'), b'-_').decode('ascii')

    def _handle_client_request(self) -> NoReturn:
        """
        Handles a client request
        """
        BackupScheduler.logger.debug("Handling user command")
        request = self.pipe_request_read.recv()
        data = None
        try:
            command, args = request
            data, tasks_changed = self.command_parser.parse_command(command, args)
            if tasks_changed:
                self._reload_schedule()
                self._clean_backup_path()
        except Exception as e:
            BackupScheduler.logger.exception("Error handling client request")
            self.pipe_request_answer.send(("Error %s:" % str(e), data))
        self.pipe_request_answer.send(("OK", data))

    def _dispatch_running_tasks(self):
        """
        Handles running tasks
        """
        now_running_tasks = {}
        for node_data, task in self.running_tasks.items():
            if not task.is_running():
                if task.backup_is_correct():
                    ft = FinishedTask(result_path=task.write_file_path,
                                      kb_size=os.path.getsize(task.write_file_path) / 1024,
                                      timestamp=datetime.now(),
                                      checksum=BackupFile(task.write_file_path).get_hash())
                    self.database.register_finished_task(node_data[0], node_data[1], ft)
                    BackupScheduler.logger.info("Backup for node %s and path %s finished succesfully" % node_data)
                    self._reload_schedule()
                    self._clean_backup_path()
                elif task.backup_is_same():
                    ft = self.database.get_node_finished_tasks(node_data[0], node_data[1])[0]
                    ft = FinishedTask(result_path=ft.result_path,
                                      kb_size=ft.kb_size,
                                      timestamp=datetime.now(),
                                      checksum=ft.checksum)
                    self.database.register_finished_task(node_data[0], node_data[1], ft)
                    BackupScheduler.logger.info("Backup for node %s and path %s finished succesfully" % node_data)
                    self._reload_schedule()
                    self._clean_backup_path()
                else:
                    BackupScheduler.logger.error("Backup for node %s and path %s failed" % node_data)
            else:
                now_running_tasks[node_data] = task
        self.running_tasks = now_running_tasks

    def _run_new_tasks(self):
        """
        Handles the schedule to run new tasks
        """
        for sched_task in self.schedule:
            if (sched_task.node_name, sched_task.node_path) in self.running_tasks:
                continue
            if sched_task.should_run() and (
                    sched_task.node_name, sched_task.node_path, sched_task.last_checksum) not in self.task_queue:
                self.task_queue.appendleft((sched_task.node_name, sched_task.node_path, sched_task.last_checksum))
            number_of_running_tasks = len(self.running_tasks)
            for queued_task in range(min(self.max_processes - number_of_running_tasks, len(self.task_queue))):
                node_name, node_path, last_checksum = self.task_queue.pop()
                node_address, node_port = self.database.get_node_address(node_name)
                write_file_path = WRITE_FILE_PATH_TEMPLATE % (self.backup_path,
                                                              datetime.now().replace(tzinfo=timezone.utc).timestamp(),
                                                              node_name,
                                                              self.safe_base64(node_path))
                node_handler = NodeHandlerProcess(node_address=node_address,
                                                  node_path=node_path,
                                                  node_port=node_port,
                                                  write_file_path=write_file_path,
                                                  previous_checksum=last_checksum)
                p = Process(target=node_handler)
                p.start()
                BackupScheduler.logger.debug("Backup order for node %s and path %s launched" %
                                             (node_name, node_path))
                self.running_tasks[(node_name, node_path)] = RunningTask(write_file_path, p)

    def __call__(self) -> NoReturn:
        """
        Code for running the main loop in the main process

        The process works this way, while true:
            1. Checks for SECONDS_TO_WAIT_CLIENT the pipe from the client controller to see if theres an order to execute
                1.1 If theres an order to execute it runs it
                1.2 The answer to order is sent through self.pipe_request_answer
            2. For each node handler process that ended:
                If there is a .CORRECT it deletes it and registers the backup in the database
                If there is no .CORRECT it deletes all files associated with that process backup
            3. Launches new node handler processes for the backups that need to be done according last
            backup time, actual time and if there isnt a backup already running for that node and path

        If other error happens and the process must die:
            * Kill all other processes then dies
        """
        try:
            self._reload_schedule()
            self._clean_backup_path()
            while True:
                if self.pipe_request_read.poll(SECONDS_TO_WAIT_CLIENT):
                    self._handle_client_request()
                self._dispatch_running_tasks()
                self._run_new_tasks()
        except Exception as e:
            BackupScheduler.logger.exception("Aborting backup scheduler")
            self.pipe_request_answer.close()
            for t in self.running_tasks.values():
                if t.process.is_alive():
                    t.process.termitate()
