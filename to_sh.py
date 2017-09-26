#! /usr/bin/env python3.6

import subprocess
import getopt
import sys
import re
import os
import logging

import crow.config
import crow.sysenv
from crow.exceptions import CROWException
from crow.tools import str_to_posix_sh
from collections import Mapping

logger=logging.getLogger('CROW')
logging.basicConfig(level=logging.INFO,stream=sys.stderr)

UNSET_VARIABLE=object()
SUCCESS=object()
FAILURE=object()

class EpicFail(Exception): pass

class ProcessArgs(object):
    def __init__(self,quiet,args):
        self.quiet=bool(quiet)
        self.args = args
        self.config = None
        self.scope = None
        self.float_format = '%f'
        self.int_format = '%d'
        self.true_string = 'YES'
        self.false_string = 'NO'
        self.null_string=UNSET_VARIABLE
        self.done_with_files=False
        self.files=list()
        self.export_vars=False
        self.have_expanded=False
        self.have_handled_vars=False
        self.runner=None

    def set_bool_format(self,value):
        yes_no = value.split(',')
        if len(yes_no) != 2:
            raise ValueError(f'{value}: bool format must be two '
                             'comma-separated values ("YES,NO")')
        self.true_string=yes_no[0]
        self.false_string=yes_no[1]

    def set_runner(self,expr='doc.platform.parallelism'):
        settings=self.eval_expr(expr)
        self.runner=crow.sysenv.get_parallelism(settings.name,settings)

    def run_expr(self,expr,check=False):
        cmd=self.eval_expr(expr)
        if hasattr(cmd,'index') and hasattr(cmd[0],'keys'):
            # List of dicts, so it is an MPI command
            if self.runner is None: self.set_runner()
            self.runner.run(cmd,check=check)
        else:
            sh=crow.sysenv.ShellCommand.from_object(cmd)
            print(sh)
            sh.run(check=check)

    def eval_expr(self,expr):
        globals={}
        if hasattr(self.scope,'_globals'):
            globals=self.scope._globals()
        elif hasattr(self.config,'_globals'):
            globals=self.config._globals()
        return eval(expr,globals,self.scope)

    def exec_str(self,expr):
        globals={}
        if hasattr(self.scope,'_globals'):
            globals=self.scope._globals()
        elif hasattr(self.config,'_globals'):
            globals=self.config._globals()
        exec(expr,globals,self.scope)

    def set_int_format(self,value):
        test=value%3
        self.int_format=value

    def set_float_format(self,value):
        test=value%1.1
        self.float_format=value

    def set_null_string(self,value):
        self.null_string=value

    def set_scope(self,value):
        self.scope=self.config
        result=self.eval_expr(value)
        if not isinstance(result,Mapping):
            raise TypeError(f'{value}: not a mapping; not a valid scope '
                            f'(is a {type(result).__name__})')
        self.scope=result
        crow.config.validate(result,'execution')

    def set_export_vars(self,value):
        if value.lower()[0] in [ 'y', 't' ]:
            self.export_vars=True
        elif value.lower()[0] in [ 'n', 'f' ]:
            self.export_vars=False
        else:
            raise ValueError(f'{value}: not a logical (YES, NO)')

    def format_object(self,obj):
        if obj is True:
            return self.true_string
        elif obj is False:
            return self.false_string
        elif obj is None:
            return self.null_format
        elif isinstance(obj,str):
            return obj
        elif isinstance(obj,float):
            return self.float_format%obj
        elif isinstance(obj,int):
            return self.int_format%obj
        return NotImplemented

    def read_files(self):
        config=crow.config.from_file(*self.files)
        self.config = config
        self.scope = config
        self.done_with_files=True

    def to_shell(self,var,value):
        export='export ' if self.export_vars else ''
        try:
            if var is None:
                return SUCCESS
            value=str(str_to_posix_sh(value),'ascii')
            if value is UNSET_VARIABLE:
                return f'unset {var}'
            else:
                return f'{export}{var}={value}'
        except ( NameError, AttributeError, LookupError, NameError,
                 ReferenceError, ValueError, TypeError, CROWException,
                 subprocess.CalledProcessError ) as ERR:
            logger.error(f'{arg}: {ERR!s}',exc_info=not self.quiet)
            return FAILURE

    def process_args(self):
        results=list()
        fail=False
        for arg in self.args:
            for var, value in self.process_arg(arg):
                result=self.to_shell(var,value)
                if result is FAILURE:
                    fail=True
                elif result is not SUCCESS:
                    results.append(result)
        if fail:
            raise EpicFail()
        return results

    def expand_file(self,filename):
        with open(filename,'rt') as fd:
            contents=fd.read()
        as_expr='f'+repr(contents)
        print(self.eval_expr('f'+repr(contents)))

    def process_arg(self,arg):
        m=re.match('([a-zA-Z][a-zA-Z0-9_]*):(.*)',arg)
        if m:
            if not self.done_with_files: self.read_files()
            command, value = m.groups()
            if command=='bool':         self.set_bool_format(value)
            elif command=='int':        self.set_int_format(value)
            elif command=='float':      self.set_float_format(value)
            elif command=='scope':      self.set_scope(value)
            elif command=='null':       self.set_null_string(value)
            elif command=='runner':     self.set_runner(value)
            elif command=='run_ignore': self.run_expr(value,False)
            elif command=='run':        self.run_expr(value,True)
            elif command=='apply':      self.exec_str(value)
            elif command=='import':
                for k,v in self.import_all(value):
                    yield k,v
                return
            elif command=='expand' or command=='preprocess':
                if self.have_handled_vars:
                    raise Exception(f'{arg}: cannot expand files and set '
                                    'variables in the same call.')
                self.have_expanded=True
                if command=='expand':
                    print(self.eval_expr(value))
                else:
                    self.expand_file(value)
                return
            else:
                raise ValueError(f'{command}: not a valid command '
                                 '(bool, int, float, scope, null)')
            yield None,None
            return

        m=re.match('([A-Za-z_][a-zA-Z0-9_]*)=(.*)',arg)
        if m:
            var,expr = m.groups()
            yield self.express_var(var,expr)
            return
        if self.done_with_files:
            raise ValueError('Do not understand arg: '+repr(arg))

        if os.path.isfile(arg):
            self.files.append(arg)
        elif not os.path.exists(arg) and not os.path.islink(arg):
            raise ValueError(f'{arg}: no such file')
        else:
            raise ValueError(f'{arg}: not a regular file')
        yield None,None

    def import_all(self,regex):
        for key in self.scope.keys():
            if re.match(regex,key):
                yield self.express_var(key,key)

    def express_var(self,var,expr):
        if self.have_expanded:
            raise Exception(f'{arg}: cannot expand files and set variables'
                            'in the same call.')
        self.have_handled_vars=True
        if not self.done_with_files: self.read_files()
        result=self.eval_expr(expr)
        formatted=self.format_object(result)
        if formatted is NotImplemented:
            raise TypeError(
                f'cannot convert a {type(result).__name__} '
                'to a shell expression.')
        if formatted is UNSET_VARIABLE:
            return 'unset '+var
        return var, formatted


########################################################################

if __name__ == '__main__':
    try:
        verbose=sys.argv[1]=='-v'
        pa=ProcessArgs(not verbose,sys.argv[verbose+1:])
        writeme=' '.join(pa.process_args())
        sys.stdout.write(writeme)
    except EpicFail:
        sys.stderr.write('Failure; see prior errors.\n')
        exit(1)

