"""!Internal representation types for tasks and workflows

@note Basic python concepts in use

To develop or understand this file, you must be fluent in the
following basic Python concepts:

- namedtuple
- inheritance
"""

from functools import reduce
import operator, io
from datetime import timedelta
from abc import abstractmethod
from collections import namedtuple, OrderedDict, Sequence
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from crow.config.exceptions import *
from crow.config.eval_tools import dict_eval, strcalc, multidict, from_config
from crow.tools import to_timedelta, typecheck

__all__=[ 'SuiteView', 'Suite', 'Depend', 'LogicalDependency',
          'AndDependency', 'OrDependency', 'NotDependency',
          'StateDependency', 'Dependable', 'Taskable', 'Task',
          'Family', 'Cycle', 'RUNNING', 'COMPLETED', 'FAILED',
          'TRUE_DEPENDENCY', 'FALSE_DEPENDENCY', 'SuitePath',
          'CycleExistsDependency', 'FamilyView', 'TaskView',
          'CycleView', 'Slot', 'InputSlot', 'OutputSlot', 'Message' ]

class StateConstant(object):
    def __init__(self,name):
        self.name=name
    def __repr__(self): return self.name
    def __str__(self): return self.name
RUNNING=StateConstant('RUNNING')
COMPLETED=StateConstant('COMPLETED')
FAILED=StateConstant('FAILED')

MISSING=object()
VALID_STATES=[ 'RUNNING', 'FAILED', 'COMPLETED' ]
ZERO_DT=timedelta()
EMPTY_DICT={}
SUITE_SPECIAL_KEYS=set([ 'parent', 'up', 'task_path', 'task_path_var',
                         'task_path_str', 'task_path_list' ])
SLOT_SPECIALS = SUITE_SPECIAL_KEYS|set([ 'slot', 'flow', 'actor', 'meta',
                                         'Out', 'Loc'])

class SuitePath(list):
    """!Simply a list that can be hashed."""
    def __hash__(self):
        result=0
        for element in self:
            result=result^hash(element)
        return result

class SuiteView(Mapping):
    LOCALS=set(['suite','viewed','path','parent','__cache','__globals',
                '_more_globals'])
    def __init__(self,suite,viewed,path,parent):
        # assert(isinstance(suite,Suite))
        # assert(isinstance(viewed,dict_eval))
        assert(isinstance(parent,SuiteView))
        assert(not isinstance(viewed,SuiteView))
        self.suite=suite
        # if isinstance(viewed,Task) and  'fcst' in '-'.join([str(s) for s in path]):
        #     print(path)
        #     print(viewed.keys())
        #     assert('Template' in viewed)
        #     assert('testvar' not in viewed)
        self.viewed=viewed
        self.viewed.task_path_list=path[1:]
        self.viewed.task_path_str='/'+'/'.join(path[1:])
        self.viewed.task_path_var='.'.join(path[1:])
        self.viewed._path=self.viewed.task_path_var
        if isinstance(self.viewed,Task):
            for k,v in self.viewed.items():
                v=copy(v)
                if hasattr(v,"_validate"):
                    v._validate('suite')
                self.viewed[k]=v
        if type(self.viewed) in SUITE_CLASS_MAP:
            self.viewed.up=parent
        self.path=SuitePath(path)
        self.parent=parent
        self.__cache={}
        if isinstance(self.viewed,Slot):
            locals=multidict(self.parent,self.viewed)
            globals=self.viewed._get_globals()
            for k,v in self.viewed._raw_child().items():
                if hasattr(v,'_as_dependency'): continue
                self.viewed[k]=from_config(k,v,globals,locals,self.viewed._path)

    def _globals(self):
        return self.viewed._globals()

    def __eq__(self,other):
        return self.path==other.path and self.suite is other.suite

    def __hash__(self):
        return hash(self.path)

    def has_cycle(self,dt):
        return CycleExistsDependency(to_timedelta(dt))

    def __len__(self):
        return len(self.viewed)

    def __iter__(self):
        for var in self.viewed: yield var

    def __repr__(self):
        return f'{type(self.viewed).__name__}@{self.path}'

    def __str__(self):
        s=str(self.viewed)
        if self.path[0]:
            s=f'dt=[{self.path[0]}]:'+s
        return s

    def get_trigger_dep(self):
        return self.get('Trigger',TRUE_DEPENDENCY)

    def get_complete_dep(self):
        return self.get('Complete',FALSE_DEPENDENCY)

    def get_time_dep(self):
        return self.get('Time',timedelta.min)

    def child_iter(self):
        """!Iterates over all tasks and families that are direct 
        children of this family, yielding a SuiteView of each."""
        for var,rawval in self.viewed._raw_child().items():
            if var=='up': continue
            if hasattr(rawval,'_as_dependency'): continue
            val=self[var]
            if isinstance(val,SuiteView):
                yield val

    def walk_task_tree(self):
        """!Iterates over the entire tree of descendants below this SuiteView,
        yielding a SuiteView of each."""
        for val in self.child_iter():
            yield val
            if isinstance(val,SuiteView):
                for t in val.walk_task_tree():
                    yield t

    def __contains__(self,key):
        return key in self.viewed

    def is_task(self): return isinstance(self.viewed,Task)
    def is_input_slot(self): return isinstance(self.viewed,InputSlot)
    def is_output_slot(self): return isinstance(self.viewed,OutputSlot)

    def at(self,dt):
        dt=to_timedelta(dt)
        cls=type(self)
        ret=cls(self.suite,self.viewed,
                         [self.path[0]+dt]+self.path[1:],self)
        return ret

    def __getattr__(self,key):
        if key in SuiteView.LOCALS: raise AttributeError(key)
        if key in self: return self[key]
        raise AttributeError(key)

    def __getitem__(self,key):
        assert(isinstance(key,str))
        if key in self.__cache: return self.__cache[key]
        if key not in self.viewed: raise KeyError(key)
        val=self.viewed[key]

        if isinstance(val,SuiteView):
            return val
        elif type(val) in SUITE_CLASS_MAP:
            val=self.__wrap(key,val)
        elif hasattr(val,'_as_dependency'):
            locals=multidict(self.parent,self)
            val=self.__wrap(key,val._as_dependency(
                self.viewed._globals(),locals,self.path))
        self.__cache[key]=val
        return val

    def __wrap(self,key,obj):
        if isinstance(obj,Cycle):
            # Reset path when we see a cycle
            obj=copy(obj)
            self.viewed[key]=obj
            return CycleView(self.suite,obj,self.path[:1],self)
        elif type(obj) in SUITE_CLASS_MAP:
            view_class=SUITE_CLASS_MAP[type(obj)]
            obj=copy(obj)
            self.viewed[key]=obj
            return view_class(self.suite,obj,self.path+[key],self)
        return obj

    # Dependency handling.  When this SuiteView is wrapped around a
    # Task or Family, these operators will generate dependencies.

    def __and__(self,other):
        dep=as_dependency(other)
        if dep is NotImplemented: return dep
        return AndDependency(as_dependency(self),dep)
    def __or__(self,other):
        dep=as_dependency(other)
        if dep is NotImplemented: return dep
        return OrDependency(as_dependency(self),dep)
    def __invert__(self):
        return NotDependency(StateDependency(self,COMPLETED))
    def is_running(self):
        return StateDependency(self,RUNNING)
    def is_failed(self):
        return StateDependency(self,FAILED)
    def is_completed(self):
        return StateDependency(self,COMPLETED)

class SlotView(SuiteView):
    def __init__(self,suite,viewed,path,parent,search=MISSING):
        super().__init__(suite,copy(viewed),path,parent)
        assert(isinstance(path,Sequence))
        if search is MISSING: 
            self.__search={}
            return
        for naughty in search:
            if naughty in SLOT_SPECIALS:
                pathstr='.'.join(path[1:])
                raise ValueError(
                    f'{pathstr}: {naughty}: cannot be in meta')
        self.__search=dict()
    def get_actor_path(self):
        return '.'.join(self.path[1:-1])
    def get_slot_name(self):
        return self.path[-1]
    def get_search(self):
        return self.__search
    @abstractmethod
    def get_flow_name(self): pass
    def slot_iter(self):
        cls=type(self)
        arrays=list()
        names=list()
        for k in self:
            if k in SLOT_SPECIALS: continue
            v=self[k]
            if not isinstance(v,Sequence): continue
            if isinstance(v,str): continue
            names.append(k)
            arrays.append(v)
        if not names:
            yield self
            return
        lens=[ len(a) for a in arrays ]
        index=[ 0 ] * len(lens)
        while True:
            result=cls(self.suite,copy(self.viewed),self.path,
                       self.parent,self.__search)
            for i in range(len(arrays)):
                result.viewed[names[i]]=self[names[i]][index[i]]
            yield result
            for i in range(len(arrays)):
                index[i]+=1
                if index[i]<lens[i]: break
                if i == len(arrays)-1: return
                index[i]=0
    def get_meta(self):
        d=dict()
        for k in self:
            if k in SLOT_SPECIALS: continue
            v=self[k]
            if type(v) in [ int, float, bool, str ]:
                d[k]=v
        return d
    def __call__(self,**kwargs):
        cls=type(self)
        return cls(self.suite,self.viewed,self.path,
                   self.parent,kwargs)
    def __invert__(self): raise TypeError('cannot invert a Slot')
    def is_running(self): raise TypeError('data cannot run')
    def is_failed(self): raise TypeError('data cannot run')
    def is_completed(self): raise TypeError('data cannot run')

class CycleView(SuiteView): pass
class TaskView(SuiteView): pass
class FamilyView(SuiteView): pass
class InputSlotView(SlotView):
    def get_output_slot(self): return self.Out
    def get_flow_name(self): return 'I'
class OutputSlotView(SlotView):
    def get_flow_name(self): return 'O'
    def get_slot_location(self): return self.Loc

class Suite(SuiteView):
    def __init__(self,suite,more_globals=EMPTY_DICT):
        if not isinstance(suite,Cycle):
            raise TypeError('The top level of a suite must be a Cycle not '
                            'a %s.'%(type(suite).__name__,))
        viewed=deepcopy(suite)
        globals=dict(viewed._globals())
        assert(globals['tools'] is not None)
        globals.update(suite=self,
                       RUNNING=RUNNING,COMPLETED=COMPLETED,
                       FAILED=FAILED)
        self._more_globals=dict(more_globals)

        globals.update(self._more_globals)
        super().__init__(self,viewed,[ZERO_DT],self)
        viewed._recursively_set_globals(globals)
    def has_cycle(self,dt):
        return CycleExistsDependency(to_timedelta(dt))
    def make_empty_copy(self,more_globals=EMPTY_DICT):
        suite_copy=deepcopy(self)
        new_more_globals=copy(suite_copy._more_globals)
        new_more_globals.update(more_globals)
        return Suite(suite_copy,new_more_globals)
    def update_globals(self,*args,**kwargs):
        globals=self.viewed._get_globals()
        globals.update(*args,**kwargs)
        self.viewed._recursively_set_globals(globals)

class Message(str):
    def _as_dependency(self,globals,locals,path):
        try:
            return eval(self,globals,locals)
        except(SyntaxError,TypeError,KeyError,NameError,IndexError,AttributeError) as ke:
            raise DependError(f'!Message {self}: {ke}')

class Depend(str):
    def _as_dependency(self,globals,locals,path):
        try:
            result=eval(self,globals,locals)
            result=as_dependency(result,path)
            return result
        except(SyntaxError,TypeError,KeyError,NameError,IndexError,AttributeError) as ke:
            raise DependError(f'!Depend {self}: {ke}')

def as_dependency(obj,path=MISSING,state=COMPLETED):
    """!Converts the containing object to a State.  Action objects are
    compared to the "complete" state."""
    if isinstance(obj,SuiteView) and not isinstance(obj,SlotView):
         return StateDependency(obj,state)
    if isinstance(obj,LogicalDependency): return obj
    raise TypeError(
        f'{type(obj).__name__} is not a valid type for a dependency')
    return NotImplemented

class LogicalDependency(object):
    def __invert__(self):          return NotDependency(self)
    def __contains__(self,dep):    return False
    def __and__(self,other):
        if other is FALSE_DEPENDENCY: return other
        if other is TRUE_DEPENDENCY: return self
        dep=as_dependency(other)
        if dep is NotImplemented: raise TypeError(other)
        return AndDependency(self,dep)
    def __or__(self,other):
        if other is TRUE_DEPENDENCY: return other
        if other is FALSE_DEPENDENCY: return self
        dep=as_dependency(other)
        if dep is NotImplemented: raise TypeError(other)
        return OrDependency(self,dep)
    @abstractmethod
    def copy_dependencies(self): pass
    @abstractmethod
    def add_time(self,dt): pass

class AndDependency(LogicalDependency):
    def __init__(self,*args):
        if not args: raise ValueError('Tried to create an empty AndDependency')
        self.depends=list(args)
        for dep in self.depends:
            typecheck('Dependencies',dep,LogicalDependency)
    def __len__(self):     return len(self.depends)
    def __str__(self):     return '( '+' & '.join([str(r) for r in self])+' )'
    def __repr__(self):    return f'AndDependency({repr(self.depends)})'
    def __hash__(self):    return reduce(operator.xor,[hash(d) for d in self])
    def __contains__(self,dep):
        return dep in self.depends
    def __and__(self,other):
        if other is TRUE_DEPENDENCY: return self
        if other is FALSE_DEPENDENCY: return other
        if isinstance(other,AndDependency):
            return AndDependency(*(self.depends+other.depends))
        dep=as_dependency(other)
        if dep is NotImplemented: return dep
        return AndDependency(*(self.depends+[dep]))
    def __iter__(self):
        for dep in self.depends:
            yield dep
    def __eq__(self,other):
        return isinstance(other,AndDependency) and self.depends==other.depends
    def copy_dependencies(self):
        return AndDependency(*[ dep.copy_dependencies() for dep in self ])
    def add_time(self,dt):
        for dep in self:
            dep.add_time(dt)

class OrDependency(LogicalDependency):
    def __init__(self,*args):
        if not args: raise ValueError('Tried to create an empty OrDependency')
        self.depends=list(args)
        for dep in self.depends:
            typecheck('A dependency',dep,LogicalDependency)
    def __str__(self):     return '( '+' | '.join([str(r) for r in self])+' )'
    def __repr__(self):    return f'OrDependency({repr(self.depends)})'
    def __len__(self):     return len(self.depends)
    def __hash__(self):    return reduce(operator.xor,[hash(d) for d in self])
    def __contains__(self,dep):
        return dep in self.depends
    def __or__(self,other):
        if other is FALSE_DEPENDENCY: return self
        if other is TRUE_DEPENDENCY: return other
        if isinstance(other,OrDependency):
            return OrDependency(*(self.depends+other.depends))
        dep=as_dependency(other)
        if dep is NotImplemented: return dep
        return OrDependency(*(self.depends+[dep]))
    def __iter__(self):
        for dep in self.depends:
            yield dep
    def __eq__(self,other):
        return isinstance(other,OrDependency) and self.depends==other.depends
    def copy_dependencies(self):
        return OrDependency(*[ dep.copy_dependencies() for dep in self ])
    def add_time(self,dt):
        for dep in self:
            dep.add_time(dt)

class NotDependency(LogicalDependency):
    def __init__(self,depend):
        typecheck('A dependency',depend,LogicalDependency)
        self.depend=depend
    def __invert__(self):        return self.depend
    def __str__(self):           return f'~ {self.depend}'
    def __repr__(self):          return f'NotDependency({repr(self.depend)})'
    def __iter__(self):          yield self.depend
    def __hash__(self):          return hash(self.depend)
    def __contains__(self,dep):  return self.depend==dep
    def add_time(self,dt):       self.depend.add_time(dt)
    def __eq__(self,other):
        return isinstance(other,NotDependency) and self.depend==other.depend
    def copy_dependencies(self):
        return NotDependency(self.depend.copy_dependencies())

class CycleExistsDependency(LogicalDependency):
    def __init__(self,dt):        self.dt=dt
    def __repr__(self):           return f'cycle_exists({self.dt})'
    def __hash__(self):           return hash(self.dt)
    def add_time(self,dt):        self.dt+=dt
    def copy_dependencies(self):  return CycleExistsDependency(self.dt)
    def __eq__(self,other):
        return isinstance(other,CycleExistsDependency) and self.dt==other.dt

class StateDependency(LogicalDependency):
    def __init__(self,view,state):
        if state not in [ COMPLETED, RUNNING, FAILED ]:
            raise TypeError('Invalid state.  Must be one of the constants '
                            'COMPLETED, RUNNING, or FAILED')
        typecheck('view',view,SuiteView)
        if isinstance(view,SlotView):
            raise NotImplementedError('Data dependencies are not implemented')
        self.view=view
        self.state=state
    @property
    def path(self):              return self.view.path
    def is_task(self):           return self.view.is_task()
    def __hash__(self):          return hash(self.view.path)^hash(self.state)
    def copy_dependencies(self): return StateDependency(self.view,self.state)
    def add_time(self,dt):
        self.view=copy(self.view)
        self.view.path[0]+=dt
    def __repr__(self):
        return f'/{"/".join([str(s) for s in self.view.path])}'\
               f'= {self.state}'
    def __eq__(self,other):
        return isinstance(other,StateDependency) \
            and other.state==self.state \
            and other.view.path==self.view.path

class TrueDependency(LogicalDependency):
    def __and__(self,other):     return other
    def __or__(self,other):      return self
    def __invert__(self):        return FALSE_DEPENDENCY
    def __eq__(self,other):      return isinstance(other,TrueDependency)
    def __hash__(self):          return 1
    def __copy__(self):          return TRUE_DEPENDENCY
    def __deepcopy__(self):      return TRUE_DEPENDENCY
    def copy_dependencies(self): return TRUE_DEPENDENCY
    def __repr__(self):          return 'TRUE_DEPENDENCY'
    def __str__(self):           return 'TRUE'
    def add_time(self,dt):       pass

class FalseDependency(LogicalDependency):
    def __and__(self,other):     return self
    def __or__(self,other):      return other
    def __invert__(self):        return TRUE_DEPENDENCY
    def __eq__(self,other):      return isinstance(other,FalseDependency)
    def __hash__(self):          return 0
    def __copy__(self):          return FALSE_DEPENDENCY
    def __deepcopy__(self):      return FALSE_DEPENDENCY
    def copy_dependencies(self): return FALSE_DEPENDENCY
    def __repr__(self):          return 'FALSE_DEPENDENCY'
    def __str__(self):           return 'FALSE'
    def add_time(self,dt):       pass

TRUE_DEPENDENCY=TrueDependency()
FALSE_DEPENDENCY=FalseDependency()

class Dependable(dict_eval):
    def __str__(self):
        sio=io.StringIO()
        sio.write(f'{type(self).__name__}@{self._path}')
        sio.write('{')
        first=True
        for k,v in self._raw_child().items():
            if k not in SUITE_SPECIAL_KEYS:
                sio.write(f'{", " if not first else ""}{k}={v!r}')
                first=False
        sio.write('}')
        v=sio.getvalue()
        sio.close()
        return v

class Slot(Dependable): pass
class InputSlot(Slot): pass
class OutputSlot(Slot): pass

class Taskable(Dependable): pass
class Task(Taskable): pass
class Family(Taskable): pass
class Cycle(dict_eval): pass

class TaskArray(Taskable):
    def __init__(self,*args,**kwargs):
        super().init(*args,**kwargs)
        Index=self['Index']
        varname=Index[0]
        if not isinstance(varname,str):
            raise TypeError('Index first argument should be a string variable '
                            'name not a %s'%(type(varname.__name__),))
        values=Index[1]
        if not isinstance(values,Sequence):
            raise TypeError('Index second argument should be a sequence '
                            'name not a %s'%(type(values.__name__),))
        self.__instances=[MISSING]*len(values)
    @property
    def index_name(self):
        return self['Index'][0]
    @property
    def index_count(self):
        return len(self['Index'][1])
    def index_keys(self):
        keys=self['Index'][1]
        for k in keys: yield k
    def index_items(self):
        varname=self.index_name
        keys=self['Index'][1]
        for i in len(keys):
            yield keys[i],self.__for_index(i,varname,key)
    def for_index(self,i):
        if self.__instances[i] is not MISSING:
            return self.__instances[i]
        varname=self.index_name
        keys=self['Index'][1]
        return self.__for_index(i,varname,key)
    def __for_index(self,i,varname,key):
        the_copy=Family(self._raw_child())
        the_copy[varname]=key



SUITE_CLASS_MAP={ Task:TaskView, Family: FamilyView, 
                  OutputSlot: OutputSlotView, InputSlot:InputSlotView }
