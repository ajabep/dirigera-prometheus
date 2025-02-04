from prometheus_client import multiprocess

def child_exit(server, worker):  # pylint: disable=unused-argument
    multiprocess.mark_process_dead(worker.pid)
