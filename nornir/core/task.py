import logging
import traceback
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Union, cast

from nornir.core.exceptions import NornirExecutionError
from nornir.core.exceptions import NornirSubTaskError

from typing_extensions import Protocol

if TYPE_CHECKING:
    from nornir.core import Nornir
    from nornir.core.inventory import Host


class TaskFunc(Protocol):
    @property
    def __name__(self) -> str:
        ...

    def __call__(self, task: "Task", **kwargs: Dict[str, Any]) -> Any:
        ...


class Task(object):
    """
    A task is basically a wrapper around a function that has to be run against multiple devices.
    You won't probably have to deal with this class yourself as
    :meth:`nornir.core.Nornir.run` will create it automatically.

    Arguments:
        task (callable): function or callable we will be calling
        name (``string``): name of task, defaults to ``task.__name__``
        severity_level (logging.LEVEL): Severity level associated to the task
        **kwargs: Parameters that will be passed to the ``task``

    Attributes:
        task (callable): function or callable we will be calling
        name (``string``): name of task, defaults to ``task.__name__``
        params: Parameters that will be passed to the ``task``.
        self.results (:obj:`nornir.core.task.MultiResult`): Intermediate results
        host (:obj:`nornir.core.inventory.Host`): Host we are operating with. Populated right
          before calling the ``task``
        nornir(:obj:`nornir.core.Nornir`): Populated right before calling
          the ``task``
        severity_level (logging.LEVEL): Severity level associated to the task
    """

    def __init__(
        self,
        task: TaskFunc,
        name: Optional[str] = None,
        severity_level: int = logging.INFO,
        **kwargs: Dict[str, Any]
    ) -> None:
        self.name = name or task.__name__
        self.task = task
        self.params = kwargs
        self.results = MultiResult(self.name)
        self.severity_level = severity_level

    def __repr__(self) -> str:
        return self.name

    def start(self, host: "Host", nornir: "Nornir") -> Union["MultiResult", "Result"]:
        """
        Run the task for the given host.
        """
        self.host = host
        self.nornir = nornir

        logger = logging.getLogger(__name__)
        try:
            logger.info("{}: {}: running task".format(self.host.name, self.name))
            r = self.task(self, **self.params)
            if not isinstance(r, Result):
                r = Result(host=host, result=r)

        except NornirSubTaskError as e:
            tb = traceback.format_exc()
            logger.error("{}: {}".format(self.host, tb))
            r = Result(host, exception=e, result=str(e), failed=True)

        except Exception as e:
            tb = traceback.format_exc()
            logger.error("{}: {}".format(self.host, tb))
            r = Result(host, exception=e, result=tb, failed=True)

        r.name = self.name
        r.severity_level = logging.ERROR if r.failed else self.severity_level

        self.results.insert(0, r)
        return self.results

    def run(self, task: TaskFunc, **kwargs: Any) -> Union["Result", "MultiResult"]:
        """
        This is a utility method to call a task from within a task. For instance:

            def grouped_tasks(task):
                task.run(my_first_task)
                task.run(my_second_task)

            nornir.run(grouped_tasks)

        This method will ensure the subtask is run only for the host in the current thread.
        """
        if not self.host or not self.nornir:
            msg = (
                "You have to call this after setting host and nornir attributes. ",
                "You probably called this from outside a nested task",
            )
            raise Exception(msg)

        if "severity_level" not in kwargs:
            kwargs["severity_level"] = self.severity_level
        t = Task(task, **kwargs)
        r = t.start(self.host, self.nornir)
        self.results.append(r[0] if isinstance(r, MultiResult) else r)

        if r.failed:
            # Without this we will keep running the grouped task
            raise NornirSubTaskError(task=t, result=r)

        return r

    def is_dry_run(self, override: bool = None) -> bool:
        """
        Returns whether current task is a dry_run or not.
        """
        return cast(
            bool, override if override is not None else self.nornir.data.dry_run
        )


class Result(object):
    """
    Result of running individual tasks.

    Arguments:
        changed (bool): ``True`` if the task is changing the system
        diff (obj): Diff between state of the system before/after running this task
        result (obj): Result of the task execution, see task's documentation for details
        host (:obj:`nornir.core.inventory.Host`): Reference to the host that lead ot this result
        failed (bool): Whether the execution failed or not
        severity_level (logging.LEVEL): Severity level associated to the result of the excecution
        exception (Exception): uncaught exception thrown during the exection of the task (if any)

    Attributes:
        changed (bool): ``True`` if the task is changing the system
        diff (obj): Diff between state of the system before/after running this task
        result (obj): Result of the task execution, see task's documentation for details
        host (:obj:`nornir.core.inventory.Host`): Reference to the host that lead ot this result
        failed (bool): Whether the execution failed or not
        severity_level (logging.LEVEL): Severity level associated to the result of the excecution
        exception (Exception): uncaught exception thrown during the exection of the task (if any)
    """

    def __init__(
        self,
        host: "Host",
        result: Any = None,
        changed: bool = False,
        diff: str = "",
        failed: bool = False,
        exception: Optional[BaseException] = None,
        severity_level: int = logging.INFO,
        **kwargs: Dict[str, Any]
    ) -> None:
        self.result = result
        self.host = host
        self.changed = changed
        self.diff = diff
        self.failed = failed
        self.exception = exception
        self.name = None
        self.severity_level = severity_level

        self.stdout: Optional[str] = None
        self.stderr: Optional[str] = None

        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        return '{}: "{}"'.format(self.__class__.__name__, self.name)

    def __str__(self) -> str:
        if self.exception:
            return str(self.exception)

        else:
            return str(self.result)


class AggregatedResult(Dict[str, "MultiResult"]):
    """
    It basically is a dict-like object that aggregates the results for all devices.
    You can access each individual result by doing ``my_aggr_result["hostname_of_device"]``.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return "{} ({}): {}".format(
            self.__class__.__name__, self.name, super().__repr__()
        )

    @property
    def failed(self) -> bool:
        """If ``True`` at least a host failed."""
        return any([h.failed for h in self.values()])

    @property
    def failed_hosts(self) -> Dict[str, "MultiResult"]:
        """Hosts that failed during the execution of the task."""
        return {h: r for h, r in self.items() if r.failed}

    def raise_on_error(self) -> None:
        """
        Raises:
            :obj:`nornir.core.exceptions.NornirExecutionError`: When at least a task failed
        """
        if self.failed:
            raise NornirExecutionError(self)


class MultiResult(List[Result]):
    """
    It is basically is a list-like object that gives you access to the results of all subtasks for
    a particular device/task.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, name: str) -> Any:
        return getattr(self[0], name)

    def __repr__(self) -> str:
        return "{}: {}".format(self.__class__.__name__, super().__repr__())

    @property
    def failed(self) -> bool:
        """If ``True`` at least a task failed."""
        return any([h.failed for h in self])

    @property
    def changed(self) -> bool:
        """If ``True`` at least a task changed the system."""
        return any([h.changed for h in self])

    def raise_on_error(self) -> None:
        """
        Raises:
            :obj:`nornir.core.exceptions.NornirExecutionError`: When at least a task failed
        """
        if self.failed:
            raise NornirExecutionError(self)
