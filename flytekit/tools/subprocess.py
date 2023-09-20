import shlex as _schlex
import subprocess as _subprocess
import tempfile as _tempfile

from flytekit.loggers import logger


def check_call(cmd_args, **kwargs):
    if not isinstance(cmd_args, list):
        cmd_args = _schlex.split(cmd_args)

    # Jupyter notebooks hijack I/O and thus we cannot dump directly to stdout.
    with _tempfile.TemporaryFile() as std_out:
        with _tempfile.TemporaryFile() as std_err:
            ret_code = _subprocess.Popen(cmd_args, stdout=std_out, stderr=std_err, **kwargs).wait()

            # Dump sub-process' std out into current std out
            std_out.seek(0)
            logger.info(f"Output of command '{cmd_args}':\n{std_out.read()}\n")

            if ret_code != 0:
                std_err.seek(0)
                err_str = std_err.read()
                logger.error(f"Error from command '{cmd_args}':\n{err_str}\n")

                raise Exception(
                    f"Called process exited with error code: {ret_code}.  Stderr dump:\n\n{err_str}"
                )

    return 0
