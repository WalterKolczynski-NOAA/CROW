import crow.tools
import os.path
import os
import datetime
from collections import Sequence, Mapping
from crow.config.exceptions import *

class Environment(dict):
    def __getattr__(self,key):
        if key in self: return self[key]
        raise AttributeError(key)

ENV=Environment(os.environ)

def strftime(d,fmt): return d.strftime(fmt)
def YMDH(d): return d.strftime('%Y%m%d%H')
def YMD(d): return d.strftime('%Y%m%d')

def seq(start,end,step):
    return [ r for r in range(start,end+1,step) ]

def fort(value,scope='scope'):
    """!Convenience function to convert a python object to a syntax valid
    in fortran namelists.    """
    if isinstance(value,str):
        return repr(value)
    elif isinstance(value,Sequence):
        # For sequences, convert to a namelist list.
        result=[]
        for item in value:
            assert(item is not value)
            fortitem=fort(item,scope)
            result.append(fortitem)
        return ", ".join(result)
    elif isinstance(value,Mapping):
        # For mappings, assume a derived type.
        subscope_keys=[ (f'{scope}%{key}',value) for key in value ]
        return ', '.join([f'{k}={fort(v,k)}' for (k,v) in subscope_keys])
    elif value is True or value is False:
        # Booleans get a "." around them:
        return '.'+str(bool(value))+'.'
    elif isinstance(value,float):
        return '%.12g'%value
    else:
        # Anything else is converted to a string.
        return str(value)

## The CONFIG_TOOLS contains the tools available to configuration yaml
## "!calc" expressions in their "tools" variable.
CONFIG_TOOLS=crow.tools.ImmutableMapping({
    'fort':fort,
    'seq':seq,
    'panasas_gb':crow.tools.panasas_gb,
    'gpfs_gb':crow.tools.gpfs_gb,
    'basename':os.path.basename,
    'dirname':os.path.dirname,
    'abspath':os.path.abspath,
    'realpath':os.path.realpath,
    'isdir':os.path.isdir,
    'isfile':os.path.isfile,
    'islink':os.path.islink,
    'exists':os.path.exists,
    'strftime':strftime,
    'to_timedelta':crow.tools.to_timedelta,
    'YMDH':YMDH,
    'YMD':YMD,
})
