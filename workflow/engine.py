# -*- coding: utf-8 -*-
#
# This file is part of Workflow.
# Copyright (C) 2011, 2012, 2014, 2015 CERN.
#
# Workflow is free software; you can redistribute it and/or modify it
# under the terms of the Revised BSD License; see LICENSE file for
# more details.

"""Define workflow engines and exceptions."""
# we are not using the newseman logging to make this library independent
import logging
import sys
from collections import (
    Iterable,
    Callable,
)

from six import reraise, string_types

from .deprecation import deprecated
from .errors import (
    BreakFromThisLoop,

    ContinueNextToken,
    SkipToken,  # From engine_db

    HaltProcessing,
    JumpCall,

    JumpToken,
    JumpTokenBack,
    JumpTokenForward,

    StopProcessing,
    WorkflowError,
    AbortProcessing,  # From engine_db
)
from .utils import staticproperty

LOGGING_LEVEL = logging.NOTSET
LOG = None


class _Signal(object):

    def __init__(self):
        self.errored_global = False
        self.errored_engine = False

    def signals(self, eng=None):
        try:
            import workflow.signals as signals
            return signals
        except ImportError:
            import_error_msg = ("Could not import signals lib; "
                                "ignoring all future signal calls.")
            if eng and not self.errored_engine:
                eng.log.warning(import_error_msg)
                self.errored_engine = True
            elif not eng and not self.errored_global:
                import logging
                logging.warning(import_error_msg)
                self.errored_global = True

    def workflow_halted(self, eng, *args, **kwargs):
        signals = self.signals(eng)
        if signals:
            signals.workflow_halted.send(*args, **kwargs)

    def workflow_error(self, eng, *args, **kwargs):
        signals = self.signals(eng)
        if signals:
            signals.workflow_error.send(*args, **kwargs)

    def workflow_started(self, eng, *args, **kwargs):
        signals = self.signals(eng)
        if signals:
            signals.workflow_started.send(*args, **kwargs)

    def workflow_finished(self, eng, *args, **kwargs):
        signals = self.signals(eng)
        if signals:
            signals.workflow_finished.send(*args, **kwargs)


Signal = _Signal()


class MachineState(object):
    """Machine state storage.

    :Properties:

        :elem_ptr:

        As the WFE proceeds, it increments this internal counter: the
        number of the element. This pointer increases before the object is taken.

        :task_pos:

        Reserved for the array that points to the task position.
        The number there points to the task that is currently executed; when
        error happens, it will be there unchanged. The pointer is updated after
        the task finished running.
    """
    def __init__(self, elem_ptr=None, task_pos=None):
        """Initialize the state of a Workflow machine."""
        self.reset()
        if elem_ptr is not None:
            self.elem_ptr = elem_ptr
        if task_pos is not None:
            self.task_pos = task_pos

    def __setattr__(self, name, value):
        if name == 'elem_ptr' and value < -1:
            raise AttributeError("elem_ptr may not be < -1")
        super(MachineState, self).__setattr__(name, value)

    def reset(self):
        """Reset the state of the machine."""
        self.elem_ptr_reset()
        self.task_pos_reset()
        self.current_object_processed = False

    def elem_ptr_reset(self):
        """Reset `elem_ptr` to its default value."""
        self.elem_ptr = -1

    def task_pos_reset(self):
        """Reset `task_pos` to its default value."""
        self.task_pos = [0]

    @staticproperty
    def _state_keys():  # pylint: disable=no-method-argument
        return ('elem_ptr', 'task_pos', 'current_object_processed')

    def __getstate__(self):
        return {key: getattr(self, key) for key in self._state_keys}

    def __setstate__(self, state):
        for key in self._state_keys:
            setattr(self, key, state[key])


class _CallbacksDict(dict):
    """dict with informative KeyError for our use-case."""

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError as e:
            e.args = ('No workflow is registered for the key: {0}. Perhaps you '
                      'forgot to load workflows or the workflow definition for '
                      'the given key was empty?'.format(key),)
            raise


class Callbacks(object):
    """Callbacks storage and interface for workflow engines.

    The reason for interfacing for a dict is mainly to prevent cases where the
    state and the callbacks would be out of sync (eg by accidentally adding a
    callback to the beginning of a callback list).
    """

    def __init__(self):
        """Initialize the internal dictionary."""
        self._dict = _CallbacksDict()

    def get(self, key='*'):
        """Return callbacks for the given workflow.

        :param key: name of the workflow (default: '*') if you want to get all
            configured workflows pass None object as a key
        :return: list of callbacks
        """
        if key:
            return self._dict[key]
        else:
            return self._dict

    def add(self, func, key='*'):
        """Insert one callable to the stack of the callables."""
        try:
            if func:  # can be None
                self.get(key).append(func)
        except KeyError:
            self._dict[key] = []
            return self._dict[key].append(func)

    def add_many(self, list_or_tuple, key='*'):
        """Insert many callable to the stack of thec callables."""
        list_or_tuple = list(self._cleanup_callables(list_or_tuple))
        for f in list_or_tuple:
            self.add(f, key)

    @classmethod
    def _cleanup_callables(cls, callbacks):
        """Remove non-callables from the passed-in callbacks.

        ..note::
            Tuples are flattened into normal members. Only lists are nested as
            expected."""
        if callable(callbacks):
            yield callbacks  # XXX Not tested
        for x in callbacks:
            if isinstance(x, list):
                yield list(cls._cleanup_callables(x))
            elif isinstance(x, tuple):
                for fc in cls._cleanup_callables(x):
                    yield fc
            elif x is not None:
                yield x

    def clear(self, key='*'):
        """Remove tasks from the workflow engine instance, or all if no `key`."""
        try:
            del self._dict[key]
        except KeyError:
            pass

    def clear_all(self):
        """Remove tasks from the workflow engine instance, or all if no `key`."""
        self._dict.clear()

    def replace(self, funcs, key='*'):
        """Replace processing workflow with a new workflow."""
        list_or_tuple = list(self._cleanup_callables(funcs))
        self.clear(key)
        self.add_many(list_or_tuple, key)


class GenericWorkflowEngine(object):

    """Workflow engine is a Finite State Machine with memory.

    Used to execute set of methods in a specified order.

    See `docs/index.rst` for extensive examples.
    """

    def __init__(self):
        """Initialize workflow."""
        self._callbacks = Callbacks()
        self._objects = []
        self.log = self.init_logger()
        self._state = MachineState()
        self.extra_data = {}

    @property
    def callbacks(self):
        """Return the current callbacks implementation."""
        return self._callbacks

    @property
    def state(self):
        """Return the current state implementation."""
        return self._state

    @staticproperty
    def signal():  # pylint: disable=no-method-argument
        """Return the signal handler."""
        return Signal

    @staticproperty
    def processing_factory():  # pylint: disable=no-method-argument
        """Return the processing factory."""
        return ProcessingFactory

    def init_logger(self):
        """Return the appropriate logger instance."""
        # return get_logger(self.__module__ + "." + self.__class__.__name__)
        return logging.getLogger(
            "workflow.%s" % self.__class__)  # default logging


    #############################################################################
    #                                                                           #
    def continue_next_token(self):
        """Continue with the next token."""
        raise ContinueNextToken

    def stop(self):
        """Break out, stop everything (in the current `wfe`)."""
        raise StopProcessing

    def halt(self, msg="", action=None, **payload):
        """Halt the workflow (stop also any parent `wfe`).

        Halts the currently running workflow by raising HaltProcessing.

        You can provide a message and the name of an action to be taken
        (from an action in actions registry).

        :param msg: message explaining the reason for halting.
        :type msg: str

        :param action: name of valid action in actions registry.
        :type action: str

        :raises: HaltProcessing
        """
        raise HaltProcessing(msg, action, **payload)

    def break_current_loop(self):
        """Break out of the current callbacks loop."""
        self.log.debug('Break from this loop')
        raise BreakFromThisLoop
    #                                                                           #
    #############################################################################

    @staticmethod
    def jump_token(offset):
        """Jump to `offset` tokens away."""
        raise JumpToken(offset)

    def jump_call(self, offset):
        """Jump to `offset` calls (in this loop) away.

        :param offset: Number of steps to jump. May be positive or negative.
        :type offset: int
        """
        self.log.debug('We skip [%s] calls' % offset)
        raise JumpCall(offset)

    @staticmethod
    def abort():
        """Abort current workflow execution without saving object."""
        raise AbortProcessing

    @staticmethod
    def skip_token():
        """Skip current workflow object without saving it."""
        raise SkipToken

    def _pre_flight_checks(self, objects):
        """Ensure we are not out of oil."""
        # Check that objects are an iterable and populated
        if not isinstance(objects, Iterable) or isinstance(objects, string_types):
            raise WorkflowError(
                'Passed in object %s is not an iterable' % (objects.__class__))
        if not objects:
            self.log.warning('List of objects is empty. Running workflow '
                             'on empty set has no effect.')
        # Check that callbacks are populated
        if not self.callbacks._dict:
            raise WorkflowError("The callbacks are empty, did you set them?")


    def process(self, objects, stop_on_error=True, stop_on_halt=True,
                initial_run=True, reset_state=True):
        """Start processing `objects`.

        :param objects: list of objects to be processed
        :param stop_on_error: whether to stop the workflow if HaltProcessing is
            raised
        :param stop_on_error: whether to stop the workflow if WorkflowError is
            raised
        :param initial_run: whether this is the first execution of this engine

        :raises: Any exception that is not handled by the
            `transitions_exception_mapper`.
        """
        self._pre_flight_checks(objects)

        if reset_state:
            self.state.reset()

        while True:
            try:
                if initial_run:
                    initial_run = False
                    self._process(objects)
                    break
                else:
                    self.restart('next', 'first')
                    break
            except HaltProcessing:
                if stop_on_halt:
                    raise
            except WorkflowError:
                if stop_on_error:
                    raise

    # XXX: No longer static
    def callback_chooser(self, obj):
        """Choose proper callback method.

        There are possibly many workflows inside this workflow engine
        and they are meant for different types of objects, this method
        should choose and return the callbacks appropriate for the currently
        processed object.

        :param obj: currently processed object
        :return: list of callbacks to run

        .. note::
            This method is part of the engine and not part of `Callbacks` to
            grant those who wish to have their own logic here access to all the
            attributes of the engine.
        """
        if hasattr(obj, 'getFeature'):
            import warnings
            warnings.warn('Support for `getFeature` will be removed in a future'
                           'release.', DeprecationWarning)
            t = obj.getFeature('type')
            if t:
                return self.callbacks.get(t)
        else:
            # for the non-token types return default workflows
            return self.callbacks.get('*')

    def run_callbacks(self, callbacks, objects, obj, indent=0):
        """Execute callbacks in the workflow.

        :param callbacks: list of callables (may be deep nested)
        :param objects: list of processed objects
        :param obj: currently processed object
        :param indent: int, indendation level - the counter
            at the indent level is increases after the task has
            finished processing; on error it will point to the
            last executed task position.
            The position adjusting also happens after the
            task has finished.
        """
        task_pos = self.state.task_pos
        while task_pos[indent] < len(callbacks):
            was_restarted = len(task_pos) - 1 > indent
            if was_restarted:
                self.log.debug(
                    'Fast-forwarding to the position:callback = {0}:{1}'
                        .format(indent, task_pos[indent]))
                # print 'indent=%s, y=%s, y=%s, \nbefore=%s\nafter=%s' %
                # (indent, y, y[indent], callbacks, callbacks[y[indent]])
                self.run_callbacks(callbacks[task_pos[indent]], objects, obj,
                                   indent + 1)
                task_pos.pop(-1)
                task_pos[indent] += 1
                continue
            inner_callbacks = callbacks[task_pos[indent]]
            try:
                if isinstance(inner_callbacks, Iterable):
                    task_pos.append(0)
                    self.run_callbacks(inner_callbacks, objects, obj, indent + 1)
                    task_pos.pop(-1)
                    task_pos[indent] += 1
                    continue
                callback_func = inner_callbacks
                try:
                    fnc_name = callback_func.__name__
                except AttributeError:
                    fnc_name = "<Unnamed Function>"
                self.log.debug("Running ({0}{1}.) callback {2} for obj: {3}"
                               .format(indent * '-', self.state.task_pos,
                                       fnc_name, repr(obj)))
                self.processing_factory.action_mapper.before_each_callback(self, callback_func, obj)
                try:
                    self.execute_callback(callback_func, obj)
                finally:
                    self.processing_factory.action_mapper.after_each_callback(self, callback_func, obj)
            except BreakFromThisLoop:
                return
            except JumpCall as jc:
                step = jc.args[0]
                if step >= 0:
                    task_pos[indent] = min(len(callbacks), task_pos[indent] + step - 1)
                else:
                    task_pos[indent] = max(-1, task_pos[indent] + step - 1)
            task_pos[indent] += 1
        # adjust the counter so that it always points to the last successfully
        # executed task
        task_pos[indent] -= 1

    def _process(self, objects):
        """Default processing factory, will process objects in order.

        :param objects: list of objects to process
        :type objects: list

        .. note::
            If you *need* to override this, others may benefit from your ideas
            - please report with your changes.

        :param objects: list of objects (passed in by self.process())
        """
        self.processing_factory.before_processing(self, objects)
        while len(objects) - 1 > self.state.elem_ptr:
            self.state.elem_ptr += 1
            obj = objects[self.state.elem_ptr]
            self.processing_factory.before_object(self, objects, obj)
            callbacks = self.callback_chooser(obj)
            if callbacks:
                self.processing_factory.action_mapper.before_callbacks(obj, self)
                try:
                    try:
                        self.run_callbacks(callbacks, objects, obj)
                    finally:
                        self.processing_factory.action_mapper.after_callbacks(obj, self)
                except Exception as e:  # pylint: disable=broad-except
                    # Store exception info so that we can re-raise it in case we
                    # have no way of handling it.
                    exc_info = sys.exc_info()
                    try:
                        try:
                            exception_handler = getattr(
                                self.processing_factory.transition_exception_mapper,
                                e.__class__.__name__
                            )
                        except AttributeError:
                            # No handler found.
                            self.processing_factory.transition_exception_mapper.\
                                Exception(obj, self, callbacks, exc_info)
                        else:
                            exception_handler(obj, self, callbacks, exc_info)
                    except Break:
                        break
                    except Continue:
                        continue
                else:
                    self.processing_factory.after_object(self, objects, obj)
            self.state.task_pos_reset()
        self.processing_factory.after_processing(self, objects)

    def execute_callback(self, callback, obj):
        """Execute a single callback.

        Override this method to implement per-callback logging."""
        callback(obj, self)

    @property
    def current_taskname(self):
        """Get name of current task/step in the workflow (if applicable)."""
        # TODO: Use the latest key, instead of '*'.
        callback_list = self.callbacks.get('*')
        if callback_list:
            for i in self.state.task_pos:
                if not isinstance(callback_list, Callable):
                    callback_list = callback_list[i]
            if isinstance(callback_list, list):
                # With operator functions such as IF_ELSE
                # The final value is not a function, but a list.value
                # We currently then just take the __str__ of that list.
                return str(callback_list)
            return callback_list.func_name

    def restart(self, obj, task, objects=None, stop_on_error=True,
                stop_on_halt=True):
        """Restart the workflow engine at given object and task.

        Will restart the workflow engine instance at given object and task
        relative to current state.

        `obj` must be either:

        * "prev": previous object
        * "current": current object
        * "next": next object
        * "first": first object

        `task` must be either:

        * "prev": previous task
        * "current": current task
        * "next": next task
        * "first": first task

        To continue with next object from the first task:

        .. code-block:: python

                wfe.restart("next", "first")

        :param obj: the object which should be restarted
        :type obj: str

        :param task: the task which should be restarted
        :type task: str
        """
        # Note that the default behaviour of `before_processing` is to replace
        # self._objects with the new objects.
        if objects:
            new_objects = objects
        else:
            new_objects = self._objects

        self._pre_flight_checks(new_objects)

        self.log.debug("Restarting workflow from {0} object and {1} task"
                       .format(str(obj), str(task)))

        # set the point from which to start processing
        # should actually point to -1 of what we want to process
        if obj == 'prev':
            # start with the previous object
            self.state.elem_ptr -= 2
        elif obj == 'current':
            # continue with the current object
            self.state.elem_ptr -= 1
        elif obj == 'next':
            pass
        elif obj == 'first':
            self.state.elem_ptr = -1
        else:
            raise Exception('Unknown start point %s for object: %s' % obj)

        # set the task that will be executed first
        if task == 'prev':
            # the previous
            self.state.task_pos[-1] -= 1
        elif task == 'current':
            # restart the task again
            pass
        elif task == 'next':
            # continue with the next task
            self.state.task_pos[-1] += 1
        elif task == 'first':
            self.state.task_pos = [0]
        else:
            raise Exception('Unknown start point for task: %s' % obj)

        self.process(new_objects, stop_on_error=stop_on_error,
                     stop_on_halt=stop_on_halt, reset_state=False)

    # XXX Now a property
    # XXX Now returns only active objects
    @property
    def objects(self):
        """Return iterator for walking through the objects."""
        return (obj for obj in self._objects)

    @property
    def current_object(self):
        """Return the currently active DbWorkflowObject."""
        if self.state.elem_ptr < 0:
            return None
        return list(self._objects)[self.state.elem_ptr]

    @property
    def has_completed(self):
        """Return whether the engine has completed its execution."""
        if self.state.elem_ptr == -1:
            return False
        return len(self._objects) - 1 == self.state.elem_ptr and \
            self.state.current_object_processed

    @staticmethod
    @deprecated('`abortProcessing` has been replaced with `abort` and will be '
                'removed in a future release.')
    def abortProcessing():
        """Abort current workflow execution without saving object."""
        raise AbortProcessing

    @staticmethod
    @deprecated('`skipToken` has been replaced with `skip_token`')
    def skipToken():
        """Skip current workflow object without saving it."""
        raise SkipToken

    @property
    @deprecated('`store` has been replaced with `extra_data`')
    def store(self):
        return self.extra_data

    @deprecated('`setWorkflow` has been replaced with `callbacks.replace`')
    def setWorkflow(self, list_or_tuple):
        return self.callbacks.replace(list_or_tuple)

    @deprecated('`setPosition` has been with setting `state.token_pos` and '
                '`state.callback_pos` separately')
    def setPosition(self, token_pos, callback_pos):
        # """Set the internal pointers (of current state/obj).

        # :param elem_ptr: (int) index of the currently processed object
        #     After invocation, the engine will grab the next obj
        #     from the list
        # :param task_pos: (list) multidimensional one-element list
        #     that says at which level the task should restart. Example:
        #     6th branch, 2nd task = [5, 1]
        # """
        self.state.elem_ptr = elem_ptr
        self.state.task_pos = task_pos

    @deprecated('`getCallbacks` has been replaced with `callbacks.get`')
    def getCallbacks(self, key='*'):
        # """Return callbacks for the given workflow.

        # :param key: name of the workflow (default: '*')
        #         if you want to get all configured workflows
        #         pass None object as a key
        # :return: list of callbacks
        # """
        return self.callbacks.get(key=key)

    @deprecated('`addCallback` has been replaced with `callbacks.add`')
    def addCallback(self, key, func, before=None, after=None,
                    relative_weight=None):
        # """Insert one callable to the stack of the callables."""
        return self.callbacks.add(func, key)

    @deprecated('`addManyCallbacks` has been replaced with `callbacks.add_many`')
    def addManyCallbacks(self, key, list_or_tuple):
        # """Insert many callable to the stack of thec callables."""
        return self.callbacks.add_many(list_or_tuple, key)

    @deprecated('`removeAllCallbacks` has been replaced with `callbacks.clear_all`')
    def removeAllCallbacks(self):
        # """Remove all the tasks from the workflow engine instance."""
        self.callbacks.clear_all()

    @deprecated('`removeCallbacks` has been replaced with `callbacks.clear`')
    def removeCallbacks(self, key):
        # """Remove callbacks for the given `key`."""
        self.callbacks.clear(key)

    @deprecated('`removeCallbacks` has been replaced with `callbacks.clear`')
    def replaceCallbacks(self, key, funcs):
        # """Replace processing workflow with a new workflow."""
        self.callbacks.replace(key, funcs)

    @deprecated('`getCurrObjId` has been replaced with `state.token_pos`')
    def getCurrObjId(self):
        # """Return id of the currently processed object."""
        return self.state.elem_ptr

    @deprecated('`getCurrTaskId` has been replaced with `state.callback_pos`')
    def getCurrTaskId(self):
        # """Return id of the currently processed task.

        # .. note:: The return value of this method is not thread-safe.
        # """
        return self.state.task_pos

    @deprecated('`duplicate` has been deprecated in favour of the new '
                'architecture. Please read the new documentation on extending '
                'workflow')
    def duplicate(self):
        # """Duplicate workflow engine based on existing instance.

        # Instead of trying to work around any user-induced patching, we only
        # support making changes to the class by overriding properties.
        # """
        return self.__class__()

    @deprecated('`jumpTokenForward` has been replaced with `jump_token`')
    def jumpTokenForward(self, offset):
        # """Jump to `x` th token."""
        raise JumpTokenForward(offset)

    @deprecated('`jumpTokenForward` has been replaced with `jump_token`, used '
                'with a negative offset')
    def jumpTokenBack(self, offset):
        # """Return `x` tokens back - be careful with circular loops."""
        raise JumpTokenBack(offset)

    @deprecated('`jumpCallForward` has been replaced with `jump_call`')
    def jumpCallForward(self, offset):
        # """Jump to `x` th call in this loop."""
        if offset < 0:
            raise WorkflowError("JumpCallForward cannot be negative number")
        raise JumpCall(offset)

    @deprecated('`jumpCallBack` has been replaced with `jump_call`, used '
                'with a negative offset')
    def jumpCallBack(self, offset):
        # """Return `x` calls back in the current loop.

        # .. note:: Be careful with circular loop.
        # """
        if offset > 0:
            raise WorkflowError("JumpCallBack cannot be positive number")
        raise JumpCall(offset)

    @deprecated('`setVar` has been replaced with the `extra_data` dictionary')
    def setVar(self, key, what):
        # """Store the obj in the internal stack."""
        self.extra_data[key] = what

    @deprecated('`getVar` has been replaced with the `extra_data` dictionary')
    def getVar(self, key, default=None):
        # """Return named `obj` from internal stack. If not found, return `None`.
        # :param key: name of the object to return
        # :param default: if not found, what to return instead (if this arg
        # is present, the stack will be initialized with the same value)
        # :return: anything or None
        # """
        try:
            return self.extra_data[key]
        except:
            if default is not None:
                self.setVar(key, default)
                return default

    @deprecated('`getVar` has been replaced with the `extra_data` dictionary')
    def hasVar(self, key):
        # """Return True if parameter of this name is stored."""
        return key in self.extra_data

    @deprecated('`getVar` has been replaced with the `extra_data` dictionary')
    def delVar(self, key):
        # """Delete parameter from the internal storage."""
        if key in self.extra_data:
            del self.extra_data[key]

    @deprecated('`haltProcessing` has been replaced with `halt`')
    def haltProcessing(self, msg="", action=None, payload=None):
        return self.halt(msg=msg, action=action, payload=payload)

    @deprecated('`continueNextToken` has been replaced with `continue_next_token`')
    def continueNextToken(self):
        self.continue_next_token()

    @deprecated('`continueNextToken` has been replaced with `stop`')
    def stopProcessing(self):
        return self.stop()

    @deprecated('`continueNextToken` has been replaced with `break_current_loop`')
    def breakFromThisLoop(self):
        return self.break_current_loop()

    @deprecated('`jumpToken` has been replaced with `jump_token`')
    def jumpToken(self, offset):
        return self.jump_token(offset)

    @deprecated('`jumpCall` has been replaced with `jump_call`')
    def jumpCall(self, offset):
        return self.jump_call(offset)


class ActionMapper(object):

    """Actions to be taken during the execution of a processing factory."""

    @staticmethod
    def before_callbacks(obj, eng):
        """Action to do before the first callback."""
        pass

    @staticmethod
    def after_callbacks(obj, eng):
        """Action to unconditionally do after all the callbacks have completed."""
        pass

    @staticmethod
    def before_each_callback(eng, callback_func, obj):
        """Action to do before every WF callback."""
        pass

    @staticmethod
    def after_each_callback(eng, callback_func, obj):
        """Action to unconditionally do after every callback."""
        pass


class Break(Exception):
    """Request a `break` from a transition action."""
    pass


class Continue(Exception):
    """Request a `continue` from a transition action."""
    pass


class TransitionActions(object):

    """Actions to take when WorkflowTransition exceptions are raised."""

    @staticmethod
    def StopProcessing(obj, eng, callbacks, exc_info):
        """Gracefully stop the execution of the engine."""
        eng.log.debug("Processing was stopped: '%s' (object: %s)" % (
            str(eng._callbacks), repr(obj)))
        raise Break

    @staticmethod
    def HaltProcessing(obj, eng, callbacks, exc_info):
        """Interrupt the execution of the engine."""
        eng.log.debug("Processing was halted at step: %s" % eng.state)
        # Re-raise the exception, this is the only case when
        # a WFE can be completely stopped
        eng.signal.workflow_halted(eng)
        reraise(*exc_info)

    # XXX This seems to be `JumpToken(+1); Continue`, but be careful, `Continue`
    # might not work with steps other than +1
    @staticmethod
    def ContinueNextToken(obj, eng, callbacks, exc_info):
        """Action to take when ContinueNextToken is raised."""
        eng.log.debug("Stop processing for this object, "
                      "continue with next")
        eng.state.task_pos_reset()
        raise Continue

    @staticmethod
    def JumpToken(obj, eng, callbacks, exc_info):
        """Action to take when JumpToken is raised."""
        step = exc_info[1].args[0]
        if step > 0:
            eng.state.elem_ptr = min(len(eng._objects), eng.state.elem_ptr - 1 +
                                     step)
        else:
            eng.state.elem_ptr = max(-1, eng.state.elem_ptr - 1 + step)
        eng.state.task_pos_reset()

    # From engine_db
    @staticmethod
    def SkipToken(obj, eng, callbacks, exc_info):
        """Action to take when SkipToken is raised."""
        msg = "Skipped running this object: '%s' (object: %s)" % \
            (str(callbacks), repr(obj))
        eng.log.debug(msg)
        obj.log.debug(msg)
        raise Continue

    # From engine_db
    @staticmethod
    def AbortProcessing(obj, eng, callbacks, exc_info):
        """Action to take when AbortProcessing is raised."""
        msg = "Processing was aborted: '%s' (object: %s)" % \
            (str(callbacks), repr(obj))
        eng.log.debug(msg)
        obj.log.debug(msg)
        raise Break

    @staticmethod
    @deprecated('`JumpTokenForward` has been replaced with `JumpToken`')
    def JumpTokenForward(obj, eng, callbacks, step):
        """Action to take when JumpTokenForward is raised."""
        if step.args[0] < 0:
            raise WorkflowError("JumpTokenForward cannot be negative number")
        eng.log.debug('We skip [%s] objects' % step.args[0])
        TransitionActions.JumpToken(obj, eng, callbacks, step)

    @staticmethod
    @deprecated('`JumpTokenBack` has been replaced with `JumpToken` and a '
                'negative step')
    def JumpTokenBack(obj, eng, callbacks, step):
        """Action to take when JumpTokenBack is raised."""
        if step.args[0] > 0:
            raise WorkflowError("JumpTokenBack cannot be positive number")
        eng.log.debug('Warning, we go back [%s] objects' % step.args[0])
        TransitionActions.JumpToken(obj, eng, callbacks, step)

    @staticmethod
    def Exception(obj, eng, callbacks, exc_info):
        """Action to take when an unhandled exception is raised."""
        eng.signal.workflow_halted(eng)
        reraise(*exc_info)

    # From engine_db
    @staticmethod
    def SkipToken(obj, eng, callbacks, e):
        """Action to take when SkipToken is raised."""
        msg = "Skipped running this object: '%s' (object: %s)" % \
            (str(callbacks), repr(obj))
        eng.log.debug(msg)
        obj.log.debug(msg)
        raise Continue

    # From engine_db
    @staticmethod
    def AbortProcessing(obj, eng, callbacks, e):
        """Action to take when AbortProcessing is raised."""
        msg = "Processing was aborted: '%s' (object: %s)" % \
            (str(callbacks), repr(obj))
        eng.log.debug(msg)
        obj.log.debug(msg)
        raise Break


class ProcessingFactory(object):

    """Extend the engine by defining callbacks and mappers for its internals."""

    @staticproperty
    def action_mapper():  # pylint: disable=no-method-argument
        """Set a mapper for actions while processing."""
        return ActionMapper

    @staticproperty
    def transition_exception_mapper():  # pylint: disable=no-method-argument
        """Set a transition exception mapper for actions while processing."""
        return TransitionActions

    @staticmethod
    def before_processing(eng, objects):
        """Standard pre-processing callback.

        Save a pointer to the processed objects.
        """
        eng.signal.workflow_started(eng)
        eng.state.current_object_processed = False
        eng._objects = objects

    @staticmethod
    def after_processing(eng, objects):
        """Standard post-processing callback; basic cleaning."""
        eng.signal.workflow_finished(eng)
        eng.state.current_object_processed = True

    @staticmethod
    def before_object(eng, objects, obj):
        """Action to take before processing an object."""
        pass

    @staticmethod
    def after_object(eng, objects, obj):
        """Action to take after processing an object."""
        pass

# ------------------------------------------------------------- #
#                       helper methods/classes                  #
# ------------------------------------------------------------- #


def get_logger(name):
    """Create a logger with parent logger and common configuration."""
    if not name.startswith('workflow') and len(name) > len('workflow'):
        sys.stderr.write(
            "Warning: you are creating a logger without 'workflow' as a "
            "root ({0}), this means that it will not share workflow settings "
            "and cannot be administered from one place".format(name))
    if LOG:
        logger = LOG.manager.getLogger(name)
    else:
        logger = logging.getLogger(name)
        hdlr = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            '%(levelname)s %(asctime)s %(name)s:%(lineno)d    %(message)s')
        hdlr.setFormatter(formatter)
        logger.addHandler(hdlr)
        logger.setLevel(LOGGING_LEVEL)
        logger.propagate = 0
    if logger not in _loggers:
        _loggers.append(logger)
    return logger


def reset_all_loggers(level):
    """Set logging level for every active logger.

    .. note:: Beware, if the global manager level is higher, then still nothing
    will be seen. Manager level has precedence.
    """
    for l in _loggers:
        l.setLevel(level)

_loggers = []
LOG = get_logger('workflow')
