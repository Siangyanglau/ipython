"""Word completion for IPython.

This module is a fork of the rlcompleter module in the Python standard
library.  The original enhancements made to rlcompleter have been sent
upstream and were accepted as of Python 2.3, but we need a lot more
functionality specific to IPython, so this module will continue to live as an
IPython-specific utility.

Original rlcompleter documentation:

This requires the latest extension to the readline module (the
completes keywords, built-ins and globals in __main__; when completing
NAME.NAME..., it evaluates (!) the expression up to the last dot and
completes its attributes.

It's very cool to do "import string" type "string.", hit the
completion key (twice), and see the list of names defined by the
string module!

Tip: to use the tab key as the completion key, call

    readline.parse_and_bind("tab: complete")

Notes:

- Exceptions raised by the completer function are *ignored* (and
generally cause the completion to fail).  This is a feature -- since
readline sets the tty device in raw (or cbreak) mode, printing a
traceback wouldn't work well without some complicated hoopla to save,
reset and restore the tty state.

- The evaluation of the NAME.NAME... form may cause arbitrary
application defined code to be executed if an object with a
__getattr__ hook is found.  Since it is the responsibility of the
application (or the user) to enable this feature, I consider this an
acceptable risk.  More complicated expressions (e.g. function calls or
indexing operations) are *not* evaluated.

- GNU readline is also used by the built-in functions input() and
raw_input(), and thus these also benefit/suffer from the completer
features.  Clearly an interactive application can benefit by
specifying its own completer function and using raw_input() for all
its input.

- When the original stdin is not a tty device, GNU readline is never
used, and this module (and the readline module) are silently inactive.
"""

#*****************************************************************************
#
# Since this file is essentially a minimally modified copy of the rlcompleter
# module which is part of the standard Python distribution, I assume that the
# proper procedure is to maintain its copyright as belonging to the Python
# Software Foundation (in addition to my own, for all new code).
#
#       Copyright (C) 2008-2010 IPython Development Team
#       Copyright (C) 2001-2007 Fernando Perez. <fperez@colorado.edu>
#       Copyright (C) 2001 Python Software Foundation, www.python.org
#
#  Distributed under the terms of the BSD License.  The full license is in
#  the file COPYING, distributed as part of this software.
#
#*****************************************************************************
from __future__ import print_function

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

import __builtin__
import __main__
import glob
import inspect
import itertools
import keyword
import os
import re
import shlex
import sys

from IPython.core.error import TryNext
from IPython.core.prefilter import ESC_MAGIC
from IPython.utils import generics, io
from IPython.utils.frame import debugx
from IPython.utils.dir2 import dir2

#-----------------------------------------------------------------------------
# Globals
#-----------------------------------------------------------------------------

# Public API
__all__ = ['Completer','IPCompleter']

if sys.platform == 'win32':
    PROTECTABLES = ' '
else:
    PROTECTABLES = ' ()'

#-----------------------------------------------------------------------------
# Main functions and classes
#-----------------------------------------------------------------------------

def protect_filename(s):
    """Escape a string to protect certain characters."""
    
    return "".join([(ch in PROTECTABLES and '\\' + ch or ch)
                    for ch in s])


def mark_dirs(matches):
    """Mark directories in input list by appending '/' to their names."""
    out = []
    isdir = os.path.isdir
    for x in matches:
        if isdir(x):
            out.append(x+'/')
        else:
            out.append(x)
    return out


def single_dir_expand(matches):
    "Recursively expand match lists containing a single dir."

    if len(matches) == 1 and os.path.isdir(matches[0]):
        # Takes care of links to directories also.  Use '/'
        # explicitly, even under Windows, so that name completions
        # don't end up escaped.
        d = matches[0]
        if d[-1] in ['/','\\']:
            d = d[:-1]

        subdirs = os.listdir(d)
        if subdirs:
            matches = [ (d + '/' + p) for p in subdirs]
            return single_dir_expand(matches)
        else:
            return matches
    else:
        return matches


class Bunch(object): pass


class CompletionSplitter(object):
    """An object to split an input line in a manner similar to readline.

    By having our own implementation, we can expose readline-like completion in
    a uniform manner to all frontends.  This object only needs to be given the
    line of text to be split and the cursor position on said line, and it
    returns the 'word' to be completed on at the cursor after splitting the
    entire line.

    What characters are used as splitting delimiters can be controlled by
    setting the `delims` attribute (this is a property that internally
    automatically builds the necessary """

    # Private interface
    
    # A string of delimiter characters.  The default value makes sense for
    # IPython's most typical usage patterns.
    _delims = ' \t\n`!@#$^&*()=+[{]}\\|;:\'",<>?'

    # The expression (a normal string) to be compiled into a regular expression
    # for actual splitting.  We store it as an attribute mostly for ease of
    # debugging, since this type of code can be so tricky to debug.
    _delim_expr = None

    # The regular expression that does the actual splitting
    _delim_re = None

    def __init__(self, delims=None):
        delims = CompletionSplitter._delims if delims is None else delims
        self.set_delims(delims)

    def set_delims(self, delims):
        """Set the delimiters for line splitting."""
        expr = '[' + ''.join('\\'+ c for c in delims) + ']'
        self._delim_re = re.compile(expr)
        self._delims = delims
        self._delim_expr = expr

    def get_delims(self):
        """Return the string of delimiter characters."""
        return self._delims

    def split_line(self, line, cursor_pos=None):
        """Split a line of text with a cursor at the given position.
        """
        l = line if cursor_pos is None else line[:cursor_pos]
        return self._delim_re.split(l)[-1]


class Completer(object):
    def __init__(self, namespace=None, global_namespace=None):
        """Create a new completer for the command line.

        Completer([namespace,global_namespace]) -> completer instance.

        If unspecified, the default namespace where completions are performed
        is __main__ (technically, __main__.__dict__). Namespaces should be
        given as dictionaries.

        An optional second namespace can be given.  This allows the completer
        to handle cases where both the local and global scopes need to be
        distinguished.

        Completer instances should be used as the completion mechanism of
        readline via the set_completer() call:

        readline.set_completer(Completer(my_namespace).complete)
        """

        # Don't bind to namespace quite yet, but flag whether the user wants a
        # specific namespace or to use __main__.__dict__. This will allow us
        # to bind to __main__.__dict__ at completion time, not now.
        if namespace is None:
            self.use_main_ns = 1
        else:
            self.use_main_ns = 0
            self.namespace = namespace

        # The global namespace, if given, can be bound directly
        if global_namespace is None:
            self.global_namespace = {}
        else:
            self.global_namespace = global_namespace

    def complete(self, text, state):
        """Return the next possible completion for 'text'.

        This is called successively with state == 0, 1, 2, ... until it
        returns None.  The completion should begin with 'text'.

        """
        if self.use_main_ns:
            self.namespace = __main__.__dict__
            
        if state == 0:
            if "." in text:
                self.matches = self.attr_matches(text)
            else:
                self.matches = self.global_matches(text)
        try:
            return self.matches[state]
        except IndexError:
            return None

    def global_matches(self, text):
        """Compute matches when text is a simple name.

        Return a list of all keywords, built-in functions and names currently
        defined in self.namespace or self.global_namespace that match.

        """
        #print 'Completer->global_matches, txt=%r' % text # dbg
        matches = []
        match_append = matches.append
        n = len(text)
        for lst in [keyword.kwlist,
                    __builtin__.__dict__.keys(),
                    self.namespace.keys(),
                    self.global_namespace.keys()]:
            for word in lst:
                if word[:n] == text and word != "__builtins__":
                    match_append(word)
        return matches

    def attr_matches(self, text):
        """Compute matches when text contains a dot.

        Assuming the text is of the form NAME.NAME....[NAME], and is
        evaluatable in self.namespace or self.global_namespace, it will be
        evaluated and its attributes (as revealed by dir()) are used as
        possible completions.  (For class instances, class members are are
        also considered.)

        WARNING: this can still invoke arbitrary C code, if an object
        with a __getattr__ hook is evaluated.

        """

        #print 'Completer->attr_matches, txt=%r' % text # dbg
        # Another option, seems to work great. Catches things like ''.<tab>
        m = re.match(r"(\S+(\.\w+)*)\.(\w*)$", text)

        if not m:
            return []
        
        expr, attr = m.group(1, 3)
        try:
            obj = eval(expr, self.namespace)
        except:
            try:
                obj = eval(expr, self.global_namespace)
            except:
                return []

        words = dir2(obj)
        
        try:
            words = generics.complete_object(obj, words)
        except TryNext:
            pass
        # Build match list to return
        n = len(attr)
        res = ["%s.%s" % (expr, w) for w in words if w[:n] == attr ]
        return res


class IPCompleter(Completer):
    """Extension of the completer class with IPython-specific features"""

    def __init__(self, shell, namespace=None, global_namespace=None,
                 omit__names=0, alias_table=None, use_readline=True):
        """IPCompleter() -> completer

        Return a completer object suitable for use by the readline library
        via readline.set_completer().

        Inputs:

        - shell: a pointer to the ipython shell itself.  This is needed
        because this completer knows about magic functions, and those can
        only be accessed via the ipython instance.

        - namespace: an optional dict where completions are performed.

        - global_namespace: secondary optional dict for completions, to
        handle cases (such as IPython embedded inside functions) where
        both Python scopes are visible.

        - The optional omit__names parameter sets the completer to omit the
        'magic' names (__magicname__) for python objects unless the text
        to be completed explicitly starts with one or more underscores.

        - If alias_table is supplied, it should be a dictionary of aliases
        to complete.

        use_readline : bool, optional
          If true, use the readline library.  This completer can still function
          without readline, though in that case callers must provide some extra
          information on each call about the current line."""

        Completer.__init__(self, namespace, global_namespace)

        self.magic_escape = ESC_MAGIC

        self.splitter = CompletionSplitter()

        # Readline-dependent code
        self.use_readline = use_readline
        if use_readline:
            import IPython.utils.rlineimpl as readline
            self.readline = readline
            delims = self.readline.get_completer_delims()
            delims = delims.replace(self.magic_escape,'')
            self.readline.set_completer_delims(delims)
            self.get_line_buffer = self.readline.get_line_buffer
            self.get_endidx = self.readline.get_endidx
        # /end readline-dependent code

        # List where completion matches will be stored
        self.matches = []
        self.omit__names = omit__names
        self.merge_completions = shell.readline_merge_completions
        self.shell = shell.shell
        if alias_table is None:
            alias_table = {}
        self.alias_table = alias_table
        # Regexp to split filenames with spaces in them
        self.space_name_re = re.compile(r'([^\\] )')
        # Hold a local ref. to glob.glob for speed
        self.glob = glob.glob

        # Determine if we are running on 'dumb' terminals, like (X)Emacs
        # buffers, to avoid completion problems.
        term = os.environ.get('TERM','xterm')
        self.dumb_terminal = term in ['dumb','emacs']
        
        # Special handling of backslashes needed in win32 platforms
        if sys.platform == "win32":
            self.clean_glob = self._clean_glob_win32
        else:
            self.clean_glob = self._clean_glob

        # All active matcher routines for completion
        self.matchers = [self.python_matches,
                         self.file_matches,
                         self.magic_matches,
                         self.alias_matches,
                         self.python_func_kw_matches,
                         ]
    
    # Code contributed by Alex Schmolck, for ipython/emacs integration
    def all_completions(self, text):
        """Return all possible completions for the benefit of emacs."""

        completions = []
        comp_append = completions.append
        try:
            for i in xrange(sys.maxint):
                res = self.complete(text, i, text)
                if not res:
                    break
                comp_append(res)
        #XXX workaround for ``notDefined.<tab>``
        except NameError:
            pass
        return completions
    # /end Alex Schmolck code.

    def _clean_glob(self,text):
        return self.glob("%s*" % text)

    def _clean_glob_win32(self,text):
        return [f.replace("\\","/")
                for f in self.glob("%s*" % text)]            

    def file_matches(self, text):
        """Match filenames, expanding ~USER type strings.

        Most of the seemingly convoluted logic in this completer is an
        attempt to handle filenames with spaces in them.  And yet it's not
        quite perfect, because Python's readline doesn't expose all of the
        GNU readline details needed for this to be done correctly.

        For a filename with a space in it, the printed completions will be
        only the parts after what's already been typed (instead of the
        full completions, as is normally done).  I don't think with the
        current (as of Python 2.3) Python readline it's possible to do
        better."""

        #print 'Completer->file_matches: <%s>' % text # dbg

        # chars that require escaping with backslash - i.e. chars
        # that readline treats incorrectly as delimiters, but we
        # don't want to treat as delimiters in filename matching
        # when escaped with backslash

        if text.startswith('!'):
            text = text[1:]
            text_prefix = '!'
        else:
            text_prefix = ''
            
        lbuf = self.lbuf
        open_quotes = 0  # track strings with open quotes
        try:
            lsplit = shlex.split(lbuf)[-1]
        except ValueError:
            # typically an unmatched ", or backslash without escaped char.
            if lbuf.count('"')==1:
                open_quotes = 1
                lsplit = lbuf.split('"')[-1]
            elif lbuf.count("'")==1:
                open_quotes = 1
                lsplit = lbuf.split("'")[-1]
            else:
                return []
        except IndexError:
            # tab pressed on empty line
            lsplit = ""

        if lsplit != protect_filename(lsplit):
            # if protectables are found, do matching on the whole escaped
            # name
            has_protectables = 1
            text0,text = text,lsplit
        else:
            has_protectables = 0
            text = os.path.expanduser(text)

        if text == "":
            return [text_prefix + protect_filename(f) for f in self.glob("*")]

        m0 = self.clean_glob(text.replace('\\',''))
        if has_protectables:
            # If we had protectables, we need to revert our changes to the
            # beginning of filename so that we don't double-write the part
            # of the filename we have so far
            len_lsplit = len(lsplit)
            matches = [text_prefix + text0 + 
                       protect_filename(f[len_lsplit:]) for f in m0]
        else:
            if open_quotes:
                # if we have a string with an open quote, we don't need to
                # protect the names at all (and we _shouldn't_, as it
                # would cause bugs when the filesystem call is made).
                matches = m0
            else:
                matches = [text_prefix + 
                           protect_filename(f) for f in m0]

        #print 'mm',matches  # dbg
        #return single_dir_expand(matches)
        return mark_dirs(matches)

    def magic_matches(self, text):
        """Match magics"""
        #print 'Completer->magic_matches:',text,'lb',self.lbuf # dbg
        # Get all shell magics now rather than statically, so magics loaded at
        # runtime show up too
        magics = self.shell.lsmagic()
        pre = self.magic_escape
        baretext = text.lstrip(pre)
        return [ pre+m for m in magics if m.startswith(baretext)]

    def alias_matches(self, text):
        """Match internal system aliases"""        
        #print 'Completer->alias_matches:',text,'lb',self.lbuf # dbg
        
        # if we are not in the first 'item', alias matching 
        # doesn't make sense - unless we are starting with 'sudo' command.
        if ' ' in self.lbuf.lstrip() and \
               not self.lbuf.lstrip().startswith('sudo'):
            return []
        text = os.path.expanduser(text)
        aliases =  self.alias_table.keys()
        if text == "":
            return aliases
        else:
            return [alias for alias in aliases if alias.startswith(text)]

    def python_matches(self,text):
        """Match attributes or global python names"""

        #print 'Completer->python_matches, txt=%r' % text # dbg
        if "." in text:
            try:
                matches = self.attr_matches(text)
                if text.endswith('.') and self.omit__names:
                    if self.omit__names == 1:
                        # true if txt is _not_ a __ name, false otherwise:
                        no__name = (lambda txt:
                                    re.match(r'.*\.__.*?__',txt) is None)
                    else:
                        # true if txt is _not_ a _ name, false otherwise:
                        no__name = (lambda txt:
                                    re.match(r'.*\._.*?',txt) is None)
                    matches = filter(no__name, matches)
            except NameError:
                # catches <undefined attributes>.<tab>
                matches = []
        else:
            matches = self.global_matches(text)

        return matches

    def _default_arguments(self, obj):
        """Return the list of default arguments of obj if it is callable,
        or empty list otherwise."""

        if not (inspect.isfunction(obj) or inspect.ismethod(obj)):
            # for classes, check for __init__,__new__
            if inspect.isclass(obj):
                obj = (getattr(obj,'__init__',None) or
                       getattr(obj,'__new__',None))
            # for all others, check if they are __call__able
            elif hasattr(obj, '__call__'):
                obj = obj.__call__
            # XXX: is there a way to handle the builtins ?
        try:
            args,_,_1,defaults = inspect.getargspec(obj)
            if defaults:
                return args[-len(defaults):]
        except TypeError: pass
        return []

    def python_func_kw_matches(self,text):
        """Match named parameters (kwargs) of the last open function"""

        if "." in text: # a parameter cannot be dotted
            return []
        try: regexp = self.__funcParamsRegex
        except AttributeError:
            regexp = self.__funcParamsRegex = re.compile(r'''
                '.*?' |    # single quoted strings or
                ".*?" |    # double quoted strings or
                \w+   |    # identifier
                \S         # other characters
                ''', re.VERBOSE | re.DOTALL)
        # 1. find the nearest identifier that comes before an unclosed
        # parenthesis e.g. for "foo (1+bar(x), pa", the candidate is "foo"
        tokens = regexp.findall(self.get_line_buffer())
        tokens.reverse()
        iterTokens = iter(tokens); openPar = 0
        for token in iterTokens:
            if token == ')':
                openPar -= 1
            elif token == '(':
                openPar += 1
                if openPar > 0:
                    # found the last unclosed parenthesis
                    break
        else:
            return []
        # 2. Concatenate dotted names ("foo.bar" for "foo.bar(x, pa" )
        ids = []
        isId = re.compile(r'\w+$').match
        while True:
            try:
                ids.append(iterTokens.next())
                if not isId(ids[-1]):
                    ids.pop(); break
                if not iterTokens.next() == '.':
                    break
            except StopIteration:
                break
        # lookup the candidate callable matches either using global_matches
        # or attr_matches for dotted names
        if len(ids) == 1:
            callableMatches = self.global_matches(ids[0])
        else:
            callableMatches = self.attr_matches('.'.join(ids[::-1]))
        argMatches = []
        for callableMatch in callableMatches:
            try:
                namedArgs = self._default_arguments(eval(callableMatch,
                                                         self.namespace))
            except:
                continue
            for namedArg in namedArgs:
                if namedArg.startswith(text):
                    argMatches.append("%s=" %namedArg)
        return argMatches

    def dispatch_custom_completer(self,text):
        #print "Custom! '%s' %s" % (text, self.custom_completers) # dbg
        line = self.full_lbuf        
        if not line.strip():
            return None

        event = Bunch()
        event.line = line
        event.symbol = text
        cmd = line.split(None,1)[0]
        event.command = cmd
        #print "\ncustom:{%s]\n" % event # dbg
        
        # for foo etc, try also to find completer for %foo
        if not cmd.startswith(self.magic_escape):
            try_magic = self.custom_completers.s_matches(
              self.magic_escape + cmd)            
        else:
            try_magic = []        
        
        for c in itertools.chain(self.custom_completers.s_matches(cmd),
                                 try_magic,
                                 self.custom_completers.flat_matches(self.lbuf)):
            #print "try",c # dbg
            try:
                res = c(event)
                # first, try case sensitive match
                withcase = [r for r in res if r.startswith(text)]
                if withcase:
                    return withcase
                # if none, then case insensitive ones are ok too
                text_low = text.lower()
                return [r for r in res if r.lower().startswith(text_low)]
            except TryNext:
                pass
            
        return None
               
    def complete(self, text=None, line_buffer=None, cursor_pos=None):
        """Return the state-th possible completion for 'text'.

        This is called successively with state == 0, 1, 2, ... until it
        returns None.  The completion should begin with 'text'.

        Note that both the text and the line_buffer are optional, but at least
        one of them must be given.

        Parameters
        ----------
          text : string, optional
            Text to perform the completion on.  If not given, the line buffer
            is split using the instance's CompletionSplitter object.

          line_buffer : string, optional
            If not given, the completer attempts to obtain the current line
            buffer via readline.  This keyword allows clients which are
            requesting for text completions in non-readline contexts to inform
            the completer of the entire text.

          cursor_pos : int, optional
            Index of the cursor in the full line buffer.  Should be provided by
            remote frontends where kernel has no access to frontend state.
        """
        #io.rprint('COMP1 %r %r %r' % (text, line_buffer, cursor_pos))  # dbg

        # if the cursor position isn't given, the only sane assumption we can
        # make is that it's at the end of the line (the common case)
        if cursor_pos is None:
            cursor_pos = len(line_buffer) if text is None else len(text)

        # if text is either None or an empty string, rely on the line buffer
        if not text:
            text = self.splitter.split_line(line_buffer, cursor_pos)

        # If no line buffer is given, assume the input text is all there was
        if line_buffer is None:
            line_buffer = text
        
        magic_escape = self.magic_escape
        self.full_lbuf = line_buffer
        self.lbuf = self.full_lbuf[:cursor_pos]

        if text.startswith('~'):
            text = os.path.expanduser(text)

        #io.rprint('COMP2 %r %r %r' % (text, line_buffer, cursor_pos))  # dbg

        # Start with a clean slate of completions
        self.matches[:] = []
        custom_res = self.dispatch_custom_completer(text)
        if custom_res is not None:
            # did custom completers produce something?
            self.matches = custom_res
        else:
            # Extend the list of completions with the results of each
            # matcher, so we return results to the user from all
            # namespaces.
            if self.merge_completions:
                self.matches = []
                for matcher in self.matchers:
                    self.matches.extend(matcher(text))
            else:
                for matcher in self.matchers:
                    self.matches = matcher(text)
                    if self.matches:
                        break
        # FIXME: we should extend our api to return a dict with completions for
        # different types of objects.  The rlcomplete() method could then
        # simply collapse the dict into a list for readline, but we'd have
        # richer completion semantics in other evironments.
        self.matches = sorted(set(self.matches))
        #io.rprint('COMP TEXT, MATCHES: %r, %r' % (text, self.matches)) # dbg
        return text, self.matches

    def rlcomplete(self, text, state):
        """Return the state-th possible completion for 'text'.

        This is called successively with state == 0, 1, 2, ... until it
        returns None.  The completion should begin with 'text'.

        Parameters
        ----------
          text : string
            Text to perform the completion on.

          state : int
            Counter used by readline.
        """
        if state==0:

            self.full_lbuf = line_buffer = self.get_line_buffer()
            cursor_pos = self.get_endidx()

            #io.rprint("\nRLCOMPLETE: %r %r %r" %
            #          (text, line_buffer, cursor_pos) ) # dbg

            # if there is only a tab on a line with only whitespace, instead of
            # the mostly useless 'do you want to see all million completions'
            # message, just do the right thing and give the user his tab!
            # Incidentally, this enables pasting of tabbed text from an editor
            # (as long as autoindent is off).

            # It should be noted that at least pyreadline still shows file
            # completions - is there a way around it?

            # don't apply this on 'dumb' terminals, such as emacs buffers, so
            # we don't interfere with their own tab-completion mechanism.
            if not (self.dumb_terminal or line_buffer.strip()):
                self.readline.insert_text('\t')
                sys.stdout.flush()
                return None

            # This method computes the self.matches array
            self.complete(text, line_buffer, cursor_pos)

            # Debug version, since readline silences all exceptions making it
            # impossible to debug any problem in the above code

            ## try:
            ##     self.complete(text, line_buffer, cursor_pos)
            ## except:
            ##     import traceback; traceback.print_exc()

        try:
            return self.matches[state]
        except IndexError:
            return None
