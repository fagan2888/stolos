from ds_commons import argparse_tools as at
from ds_commons.log import log
from scheduler import runner
from scheduler import dag_tools


from os import kill
from signal import alarm, signal, SIGALRM, SIGKILL
from subprocess import PIPE, Popen


def run(args, cwd=None, shell=False, kill_tree=True, timeout=-1, env=None):
    '''
    Run a command with a timeout after which it will be forcibly
    killed.

    # this is code by Alex Martelli
    http://stackoverflow.com/
    questions/1191374/subprocess-with-timeout/#answer-3326559
    '''
    class Alarm(Exception):
        pass

    def alarm_handler(signum, frame):
        raise Alarm

    p = Popen(args, shell=shell, cwd=cwd, stdout=PIPE, stderr=PIPE, env=env)
    if timeout != -1:
        signal(SIGALRM, alarm_handler)
        alarm(timeout)
    try:
        stdout, stderr = p.communicate()
        if timeout != -1:
            alarm(0)
    except Alarm:
        pids = [p.pid]
        if kill_tree:
            pids.extend(get_process_children(p.pid))
        for pid in pids:
            # process might have died before getting to this line
            # so wrap to avoid OSError: no such process
            try:
                kill(pid, SIGKILL)
            except OSError:
                pass
        return -9, '', ''
    return p.returncode, stdout, stderr


def get_process_children(pid):
    p = Popen('ps --no-headers -o pid --ppid %d' % pid, shell=True,
              stdout=PIPE, stderr=PIPE)
    stdout, stderr = p.communicate()
    return [int(sp) for sp in stdout.split()]


def main(ns, job_id):
    """
    A generic plugin that schedules arbitrary bash jobs using the scheduler

    Assume code is written in Python.  For Scala or R code, use another option.
    """
    ld = dict(**ns.__dict__)
    ld.update(job_id=job_id)
    log.info('Running bash job', extra=dict(**ld))
    cmd = dag_tools.get_bash_opts(ns.app_name)
    if ns.bash:
        cmd += ' '.join(ns.bash)
        log.debug(
            "Appending user-supplied bash options to defaults", extra=dict(
                app_name=ns.app_name, job_id=job_id, cmd=cmd))
    if not cmd:
        raise UserWarning(
            "You need to specify bash options or configure default bash"
            " options")

    _cmdargs = dict(**ns.__dict__)
    _cmdargs.update(dag_tools.parse_job_id(ns.app_name, job_id))
    cmd = cmd.format(**_cmdargs)

    returncode, stdout, stderr = run(
        cmd, shell=True, timeout=ns.watch)
    ld = dict(bash_returncode=returncode, stdout=stdout, stderr=stderr, **ld)
    if returncode == -9:
        runner.log_and_raise("Bash job timed out", ld)
    elif returncode != 0:
        # this raises an error and logs output:
        runner.log_and_raise("Bash job failed", ld)
    else:
        log.info("Bash job succeeded", extra=ld)


build_arg_parser = runner.build_plugin_arg_parser([at.group(
    'Bash Job Options',
    at.add_argument(
        '--bash', nargs=at.argparse.REMAINDER, help=(
            "All remaining args are passed to the bash script. ie: "
            " myscript.sh arg1 --arg2 -c=4"
        )),
    at.add_argument(
        '--watch', type=int, default=-1, help=(
            "Initiate a watchdog that will kill the process"
            " after a given seconds"
        )),
)])
