"""Elementwise functionality."""

from __future__ import division
from __future__ import absolute_import
import six
from six.moves import range
from six.moves import zip

__copyright__ = "Copyright (C) 2009 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.
"""


from pycuda.tools import context_dependent_memoize
import numpy as np
from pycuda.tools import dtype_to_ctype, VectorArg, ScalarArg
from pytools import memoize_method
from pycuda.deferred import DeferredSourceModule, DeferredSource
import pycuda._driver as _drv

class ElementwiseSourceModule(DeferredSourceModule):
    '''
    This is a ``DeferredSourceModule`` which is backwards-compatible with the
    original ``get_elwise_module`` and ``get_elwise_range_module`` (using
    ``do_range=True``).  However, this class delays the compilation of
    kernels until call-time.  If you send actual ``GPUArray`` arguments
    (instead of their ``.gpudata`` members) when calling the methods
    supported by the return value of ``get_function()``, then you get:
      * support for array-specific flat indices (i.e. for input array ``z``,
        you can index it as ``z[z_i]`` in addition to the old-style ``z[i]``)
      * support for non-contiguous (and arbitrarily-strided) arrays, but
        only if you use the array-specific indices above.
      * if do_indices=True, the N-D index of the current data item in all
        participating arrays is available in INDEX[dim] where dim goes from
        0 to NDIM - 1.
    Array-specific flat indices only really work if all the arrays using them
    are the same shape.  This shape is also used to optimize index
    calculations.  By default, the shape is taken from the first argument
    that is specified as a pointer/array, but you can override this by
    sending ``shape_arg_index=N`` where ``N`` is the zero-based index of the
    kernel argument whose shape should be used.
    By default, any VectorArg objects in ```arguments``` are assumed to be
    arrays that need index calculations.  If only some of the VectorArgs
    will be traversed element-wise, you can specify their indices (within
    ```arguments```) with ```array_arg_inds```, and the resulting kernel will
    not waste time computing indices for those.
    '''
    def __init__(self, arguments, operation,
                 name="kernel", preamble="", loop_prep="", after_loop="",
                 do_range=False,
                 shape_arg_index=None,
                 array_arg_inds=None,
                 do_indices=False,
                 order='K',
                 debug=False,
                 **compilekwargs):
        super(ElementwiseSourceModule, self).__init__(**compilekwargs)
        self._do_range = do_range
        self._do_indices = do_indices
        assert(order in 'CFK', "order must be 'F', 'C', or 'K'")
        self._order = order
        self._arg_decls = ", ".join(arg.declarator() for arg in arguments)
        if array_arg_inds is None:
            array_arg_inds = [i for i in range(len(arguments)) if isinstance(arguments[i], VectorArg)]
        self._array_arg_inds = array_arg_inds
        if shape_arg_index is None:
            self._shape_arg_index = array_arg_inds[0]
        else:
            self._shape_arg_index = shape_arg_index
        self._arg_names = tuple(argument.name for argument in arguments)
        self._array_arg_names = tuple(self._arg_names[i] for i in self._array_arg_inds)
        self._init_args = (self._arg_names, self._arg_decls, operation,
                           name, preamble, loop_prep, after_loop)
        self._init_args_repr = repr(self._init_args)
        self._debug = debug

    def _precalc_array_info(self, args, shape_arg_index):
        # 'args' is the list of actual parameters being sent to the kernel

        contigmatch = True
        arrayspecificinds = True
        shape = None
        order = None
        array_arg_inds = self._array_arg_inds
        for i in array_arg_inds:
            # is a GPUArray/DeviceAllocation
            arg = args[i]
            if not arrayspecificinds:
                continue
            if not hasattr(arg, 'shape'):
                # At least one array argument is probably sent as a
                # GPUArray.gpudata rather than the GPUArray itself,
                # so disable array-specific indices -- caller is on
                # their own.
                arrayspecificinds = False
                continue
            curshape = arg.shape
            # make strides == 0 when shape == 1
            arg.strides = tuple((np.array(curshape) > 1) * arg.strides)
            curorder = 'N'
            if contigmatch:
                if arg.flags.f_contiguous:
                    curorder = 'F'
                elif arg.flags.c_contiguous:
                    curorder = 'C'
                if curorder == 'N':
                    contigmatch = False
                if shape is None:
                    shape = curshape
                    order = curorder
                elif order != curorder:
                    contigmatch = False
            if shape_arg_index is None and shape != curshape:
                raise Exception("All input arrays to elementwise kernels must have the same shape, or you must specify the argument that has the canonical shape with shape_arg_index; found shapes %s and %s" % (shape, curshape))
            if shape_arg_index == i:
                shape = curshape

        arrayitemstrides = tuple((arg.itemsize, arg.strides) for arg in (args[i] for i in array_arg_inds))

        return (contigmatch, arrayspecificinds, shape, arrayitemstrides)


    def create_key(self, grid, block, *args):
        #print "Calling _precalc_array_info(args=%s, self._init_args[0]=%s, self._shape_arg_index=%s)\n" % (args, self._init_args[0], self._shape_arg_index)
        #precalc_init = self._precalc_array_info(args, self._shape_arg_index)
        array_arg_inds = self._array_arg_inds
        precalc_init = _drv._precalc_array_info(args, array_arg_inds, self._shape_arg_index)
        (contigmatch, arrayspecificinds, shape, arrayitemstrides) = precalc_init

        if (contigmatch or not arrayspecificinds) and not self._do_indices:
            key = self._init_args_repr
            return key, (self._init_args_repr, shape, contigmatch, arrayspecificinds, None, None, None, None, None)

        # Arrays are not contiguous or different order or need calculated indices

        if grid[1] != 1 or block[1] != 1 or block[2] != 1:
            raise Exception("Grid (%s) and block (%s) specifications should have all '1' except in the first element" % (grid, block))

        ndim = len(shape)
        arg_names = self._arg_names

        # Kernel traverses in Fortran-order by default, but if order == 'C',
        # we can just reverse the shape and strides, since the kernel is
        # elementwise.  If order == 'K', use index of minimum stride in first
        # array (or that specified by shape_arg_index) as a hint on how to
        # order the traversal of dimensions.  We could probably do something
        # smarter, like tranposing/reshaping arrays if possible to maximize
        # performance, but that is probably best done in a pre-processing step.
        # Note that this could mess up custom indexing that assumes a
        # particular traversal order, but in that case one should explicitly
        # set order to 'C' or 'F'.
        do_reverse = False
        if self._order == 'C':
            do_reverse = True
        elif self._order == 'K':
            if np.argmin(np.abs(args[self._shape_arg_index].strides)) > ndim // 2:
                # traverse dimensions in reverse order
                do_reverse = True

        # need to include grid and block in key because kernel will change
        # depending on number of threads
        key = (self._init_args_repr, shape, contigmatch, arrayspecificinds, arrayitemstrides, self._arg_decls, do_reverse, grid, block)

        return key, key

    def create_source(self, precalc, grid, block, *args):
        (_, shape, contigmatch, arrayspecificinds, arrayitemstrides, _, do_reverse, _, _) = precalc

        (arg_names, arg_decls, operation,
         funcname, preamble, loop_prep, after_loop) = self._init_args

        array_arg_inds = self._array_arg_inds
        array_arg_names = tuple(arg_names[i] for i in array_arg_inds)

        if contigmatch and not self._do_indices:
            indtype = 'unsigned'
            if self._do_range:
                indtype = 'long'

            # All arrays are contiguous and same order (or we don't know and
            # it's up to the caller to make sure it works)
            if arrayspecificinds:
                for arg_name in array_arg_names:
                    preamble = preamble + """
                        #define %s_i i
                    """ % (arg_name,)
            if self._do_range:
                loop_body = """
                  if (step < 0)
                  {
                    for (i = start + (_CTA_START + _TID)*step;
                      i > stop; i += total_threads*step)
                    {
                      %(operation)s;
                    }
                  }
                  else
                  {
                    for (i = start + (_CTA_START + _TID)*step;
                      i < stop; i += total_threads*step)
                    {
                      %(operation)s;
                    }
                  }
                """ % {
                    "operation": operation,
                }
            else:
                loop_body = """
                  for (i = _CTA_START + _TID; i < n; i += total_threads)
                  {
                    %(operation)s;
                  }
                """ % {
                    "operation": operation,
                }

            return """
                #include <pycuda-complex.hpp>

                %(preamble)s

                __global__ void %(name)s(%(arg_decls)s)
                {
                  unsigned _TID = threadIdx.x;
                  unsigned total_threads = gridDim.x*blockDim.x;
                  unsigned _CTA_START = blockDim.x*blockIdx.x;

                  %(indtype)s i;

                  %(loop_prep)s;

                  %(loop_body)s;

                  %(after_loop)s;
                }
                """ % {
                    "arg_decls": arg_decls,
                    "name": funcname,
                    "preamble": preamble,
                    "loop_prep": loop_prep,
                    "after_loop": after_loop,
                    "loop_body": loop_body,
                    "indtype": indtype,
                }

        # Arrays are not contiguous or different order or we need N-D indices

        # these are the arrays that need calculated indices
        arrayindnames = []

        # these are the arrays that match another array and can use the
        # same indices
        arrayrefnames = []

        argnamestrides = ((arg_name, itemstride[0], itemstride[1]) for (arg_name, itemstride) in zip(array_arg_names, arrayitemstrides))

        ndim = len(shape)
        numthreads = block[0] * grid[0]
        if do_reverse:
            shape = shape[::-1]
        shapearr = np.array(shape)
        block_step = np.array(shapearr)
        tmpnumthreads = numthreads
        for dimnum in range(ndim):
            newstep = tmpnumthreads % block_step[dimnum]
            tmpnumthreads = tmpnumthreads // block_step[dimnum]
            block_step[dimnum] = newstep
        arrayarginfos = []
        for (arg_name, arg_itemsize, arg_strides) in argnamestrides:
            strides = [0] * ndim
            if do_reverse:
                strides[0:len(arg_strides)] = arg_strides[::-1]
            else:
                strides[0:len(arg_strides)] = arg_strides
            elemstrides = np.array(strides) // arg_itemsize
            dimelemstrides = tuple(elemstrides * shapearr)
            blockelemstrides = tuple(elemstrides * block_step)
            elemstrides = tuple(elemstrides)
            inforef = None
            for i, arrayarginfo in enumerate(arrayarginfos):
                if elemstrides == arrayarginfo[1]:
                    inforef = i
            if inforef is None:
                arrayindnames.append(arg_name)
            else:
                arrayrefnames.append((arg_name, arrayarginfos[inforef][0]))
            arrayarginfos.append(
                (arg_name, elemstrides, dimelemstrides, blockelemstrides, inforef)
            )

        preamble = DeferredSource(preamble)
        operation = DeferredSource(operation)
        loop_prep = DeferredSource(loop_prep)
        after_loop = DeferredSource(after_loop)
        defines = DeferredSource()
        decls = DeferredSource()
        loop_preop = DeferredSource()
        loop_inds_calc = DeferredSource()
        loop_inds_inc = DeferredSource()
        loop_body = DeferredSource()

        if self._do_indices:
            defines += """#define NDIM %d""" % (ndim,)

        for definename, vals in (
                ('SHAPE', shape),
                ('BLOCK_STEP', block_step),
        ):
            for dimnum in range(ndim):
                defines += """
                    #define %s_%d %d
                """ % (definename, dimnum, vals[dimnum])
        for name, elemstrides, dimelemstrides, blockelemstrides, inforef in arrayarginfos:
            for definename, vals in (
                    ('ELEMSTRIDE', elemstrides),
                    ('DIMELEMSTRIDE', dimelemstrides),
                    ('BLOCKELEMSTRIDE', blockelemstrides),
            ):
                for dimnum in range(ndim):
                    defines += """
                        #define %s_%s_%d %d
                    """ % (definename, name, dimnum, vals[dimnum])

        decls += """
            unsigned _GLOBAL_i = _CTA_START + _TID;
        """
        for name in arrayindnames:
            decls += """
                long %s_i = 0;
            """ % (name,)

        for (name, refname) in arrayrefnames:
            defines += """
                #define %s_i %s_i
            """ % (name, refname)

        if self._do_indices:
            decls += """
                long INDEX[%d];
            """ % (ndim,)

            for dimnum in range(ndim):
                defines += """
                #define INDEX_%d (INDEX[%d])
                """ % (dimnum, dimnum)
        else:
            for dimnum in range(ndim):
                decls += """
                long INDEX_%d;
                """ % (dimnum,)

        loop_inds_calc += """
            unsigned int _TMP_GLOBAL_i = _GLOBAL_i;
        """
        for dimnum in range(ndim):
            loop_inds_calc += """
                INDEX_%d = _TMP_GLOBAL_i %% SHAPE_%d;
                _TMP_GLOBAL_i = _TMP_GLOBAL_i / SHAPE_%d;
            """ % (dimnum, dimnum,
                   dimnum)

            for name in arrayindnames:
                loop_inds_calc += """
                    %s_i += INDEX_%d * ELEMSTRIDE_%s_%d;
                """ % (name, dimnum, name, dimnum)

        for name in arrayindnames:
            loop_inds_inc += """
                    %s_i += %s;
            """ % (name,
                   " + ".join(
                       "BLOCKELEMSTRIDE_%s_%d" % (name, dimnum)
                       for dimnum in range(ndim)))
        for dimnum in range(ndim):
            loop_inds_inc += """
                    INDEX_%d += BLOCK_STEP_%d;
            """ % (dimnum, dimnum)
            if dimnum < ndim - 1:
                loop_inds_inc += """
                    if (INDEX_%d >= SHAPE_%d) {
                """ % (dimnum, dimnum)
                loop_inds_inc.indent()
                loop_inds_inc += """
                      INDEX_%d -= SHAPE_%d;
                      INDEX_%d ++;
                """ % (dimnum, dimnum,
                       dimnum + 1)
                for name in arrayindnames:
                    loop_inds_inc += """
                      %s_i += ELEMSTRIDE_%s_%d - DIMELEMSTRIDE_%s_%d;
                    """ % (name, name, dimnum + 1, name, dimnum)
                loop_inds_inc.dedent()
                loop_inds_inc += """
                    }
                """

        if self._debug:
            preamble += """
                #include <stdio.h>
            """
            loop_inds_calc += """
                if (_CTA_START == 0 && _TID == 0) {
            """
            loop_inds_calc.indent()
            loop_inds_calc += r"""
                printf("=======================\n");
                printf("CALLING FUNC %s\n");
                printf("N=%%u\n", (unsigned int)n);
            """ % (funcname,)
            for name, elemstrides, dimelemstrides, blockelemstrides, inforef in arrayarginfos:
                loop_inds_calc += r"""
                    printf("(%s) %s: ptr=0x%%lx maxoffset(elems)=%s\n", (unsigned long)%s);
                """ % (funcname, name, np.sum((np.array(shape) - 1) * np.array(elemstrides)), name)
            loop_inds_calc.dedent()
            loop_inds_calc += """
                }
            """
            indtest = DeferredSource()
            for name, elemstrides, dimelemstrides, blockelemstrides, inforef in arrayarginfos:
                if inforef is not None:
                    continue
                indtest += r"""
                    if (%s_i > %s || %s_i < 0)
                """ % (name, np.sum((np.array(shape) - 1) * np.array(elemstrides)), name)
                indtest += r"""{"""
                indtest.indent()
                indtest += r"""
                        printf("_CTA_START=%%d _TID=%%d _GLOBAL_i=%%u %s_i=%%ld %s\n", _CTA_START, _TID, _GLOBAL_i, %s_i, %s);
                """ % (name, ' '.join("INDEX_%d=%%ld" % (dimnum,) for dimnum in range(ndim)), name, ',  '.join("INDEX_%d" % (dimnum,) for dimnum in range(ndim)))
                indtest += """break;"""
                indtest.dedent()
                indtest += """}"""
            loop_preop = indtest + loop_preop
            after_loop += r"""
                if (_CTA_START == 0 && _TID == 0) {
                    printf("DONE CALLING FUNC %s\n");
                    printf("-----------------------\n");
                }
            """ % (funcname,)

        if self._do_range:
            loop_body.add("""
              if (step < 0)
              {
                for (/*void*/; _GLOBAL_i > stop; _GLOBAL_i += total_threads*step)
                {
                  %(loop_preop)s;

                  %(operation)s;

                  %(loop_inds_inc)s;
                }
              }
              else
              {
                for (/*void*/; _GLOBAL_i < stop; _GLOBAL_i += total_threads*step)
                {
                  %(loop_preop)s;

                  %(operation)s;

                  %(loop_inds_inc)s;
                }
              }
            """, format_dict={
                "loop_preop": loop_preop,
                "operation": operation,
                "loop_inds_inc": loop_inds_inc,
            })
        else:
            loop_body.add("""
              for (/*void*/; _GLOBAL_i < n; _GLOBAL_i += total_threads)
              {
                %(loop_preop)s;

                %(operation)s;

                %(loop_inds_inc)s;
              }
            """, format_dict={
                "loop_preop": loop_preop,
                "operation": operation,
                "loop_inds_inc": loop_inds_inc,
            })

        source = DeferredSource()

        source.add("""
            #include <pycuda-complex.hpp>
            #include <stdio.h>

            %(defines)s

            %(preamble)s

            __global__ void %(name)s(%(arg_decls)s)
            {

              const unsigned int _TID = threadIdx.x;
              const unsigned int total_threads = gridDim.x*blockDim.x;
              const unsigned int _CTA_START = blockDim.x*blockIdx.x;

              %(decls)s

              %(loop_prep)s;

              %(loop_inds_calc)s;

              %(loop_body)s;

              %(after_loop)s;
            }
            """, format_dict={
                "arg_decls": arg_decls,
                "name": funcname,
                "preamble": preamble,
                "loop_prep": loop_prep,
                "after_loop": after_loop,
                "defines": defines,
                "decls": decls,
                "loop_inds_calc": loop_inds_calc,
                "loop_body": loop_body,
            })

        return source


def get_elwise_module(arguments, operation,
        name="kernel", keep=False, options=None,
        preamble="", loop_prep="", after_loop="",
        **kwargs):
    return ElementwiseSourceModule(arguments, operation,
                                   name=name, preamble=preamble,
                                   loop_prep=loop_prep, after_loop=after_loop,
                                   keep=keep, options=options,
                                   **kwargs)

def get_elwise_range_module(arguments, operation,
        name="kernel", keep=False, options=None,
        preamble="", loop_prep="", after_loop="",
        **kwargs):
    return ElementwiseSourceModule(arguments, operation,
                                   name=name, preamble=preamble,
                                   loop_prep=loop_prep, after_loop=after_loop,
                                   keep=keep, options=options,
                                   do_range=True,
                                   **kwargs)

def get_elwise_kernel_and_types(arguments, operation,
        name="kernel", keep=False, options=None, use_range=False, **kwargs):
    if isinstance(arguments, str):
        from pycuda.tools import parse_c_arg
        arguments = [parse_c_arg(arg) for arg in arguments.split(",")]

    if use_range:
        arguments.extend([
            ScalarArg(np.intp, "start"),
            ScalarArg(np.intp, "stop"),
            ScalarArg(np.intp, "step"),
            ])
    else:
        arguments.append(ScalarArg(np.uintp, "n"))

    if use_range:
        module_builder = get_elwise_range_module
    else:
        module_builder = get_elwise_module

    mod = module_builder(arguments, operation, name,
            keep, options, **kwargs)

    func = mod.get_function(name)
    func.prepare("".join(arg.struct_char for arg in arguments))

    return mod, func, arguments


def get_elwise_kernel(arguments, operation,
        name="kernel", keep=False, options=None, **kwargs):
    """Return a L{pycuda.driver.Function} that performs the same scalar operation
    on one or several vectors.
    """
    mod, func, arguments = get_elwise_kernel_and_types(
            arguments, operation, name, keep, options, **kwargs)

    return func


class ElementwiseKernel:
    def __init__(self, arguments, operation,
            name="kernel", keep=False, options=None, **kwargs):

        self.gen_kwargs = kwargs.copy()
        self.gen_kwargs.update(dict(keep=keep, options=options, name=name,
            operation=operation, arguments=arguments))

    def get_texref(self, name, use_range=False):
        mod, knl, arguments = self.generate_stride_kernel_and_types(use_range=use_range)
        return mod.get_texref(name)

    @memoize_method
    def generate_stride_kernel_and_types(self, use_range):
        mod, knl, arguments = get_elwise_kernel_and_types(use_range=use_range,
                **self.gen_kwargs)

        assert [i for i, arg in enumerate(arguments)
                if isinstance(arg, VectorArg)], \
                "ElementwiseKernel can only be used with functions that " \
                "have at least one vector argument"

        return mod, knl, arguments

    def __call__(self, *args, **kwargs):
        vectors = []

        range_ = kwargs.pop("range", None)
        slice_ = kwargs.pop("slice", None)
        stream = kwargs.pop("stream", None)

        if kwargs:
            raise TypeError("invalid keyword arguments specified: "
                    + ", ".join(six.iterkeys(kwargs)))

        invocation_args = []
        mod, func, arguments = self.generate_stride_kernel_and_types(
                range_ is not None or slice_ is not None)

        for arg, arg_descr in zip(args, arguments):
            if isinstance(arg_descr, VectorArg):
                vectors.append(arg)
                invocation_args.append(arg)
            else:
                invocation_args.append(arg)

        repr_vec = vectors[0]

        if slice_ is not None:
            if range_ is not None:
                raise TypeError("may not specify both range and slice "
                        "keyword arguments")

            range_ = slice(*slice_.indices(repr_vec.size))

        if range_ is not None:
            invocation_args.append(range_.start)
            invocation_args.append(range_.stop)
            if range_.step is None:
                invocation_args.append(1)
            else:
                invocation_args.append(range_.step)

            from pycuda.gpuarray import splay
            grid, block = splay(abs(range_.stop - range_.start)//range_.step)
        else:
            block = repr_vec._block
            grid = repr_vec._grid
            invocation_args.append(repr_vec.mem_size)

        func.prepared_async_call(grid, block, stream, *invocation_args)


@context_dependent_memoize
def get_take_kernel(dtype, idx_dtype, vec_count=1):
    ctx = {
            "idx_tp": dtype_to_ctype(idx_dtype),
            "tp": dtype_to_ctype(dtype),
            "tex_tp": dtype_to_ctype(dtype, with_fp_tex_hack=True),
            }

    args = [VectorArg(idx_dtype, "idx")] + [
            VectorArg(dtype, "dest"+str(i))for i in range(vec_count)] + [
                ScalarArg(np.intp, "n")
            ]
    preamble = "#include <pycuda-helpers.hpp>\n\n" + "\n".join(
        "texture <%s, 1, cudaReadModeElementType> tex_src%d;" % (ctx["tex_tp"], i)
        for i in range(vec_count))
    body = (
            ("%(idx_tp)s src_idx = idx[idx_i];\n" % ctx)
            + "\n".join(
                "dest%d[dest%d_i] = fp_tex1Dfetch(tex_src%d, src_idx);" % (i, i, i)
                for i in range(vec_count)))

    mod = get_elwise_module(args, body, "take", preamble=preamble)
    func = mod.get_function("take")
    tex_src = [mod.get_texref("tex_src%d" % i) for i in range(vec_count)]
    func.prepare("P"+(vec_count*"P")+np.dtype(np.uintp).char, texrefs=tex_src)
    return func, tex_src


@context_dependent_memoize
def get_take_put_kernel(dtype, idx_dtype, with_offsets, vec_count=1):
    ctx = {
            "idx_tp": dtype_to_ctype(idx_dtype),
            "tp": dtype_to_ctype(dtype),
            "tex_tp": dtype_to_ctype(dtype, with_fp_tex_hack=True),
            }

    args = [
                VectorArg(idx_dtype, "gmem_dest_idx"),
                VectorArg(idx_dtype, "gmem_src_idx"),
            ] + [
                VectorArg(dtype, "dest%d" % i)
                for i in range(vec_count)
            ] + [
                ScalarArg(idx_dtype, "offset%d" % i)
                for i in range(vec_count) if with_offsets
            ] + [ScalarArg(np.intp, "n")]

    preamble = "#include <pycuda-helpers.hpp>\n\n" + "\n".join(
        "texture <%s, 1, cudaReadModeElementType> tex_src%d;" % (ctx["tex_tp"], i)
        for i in range(vec_count))

    if with_offsets:
        def get_copy_insn(i):
            return ("dest%d[dest_idx] = "
                    "fp_tex1Dfetch(tex_src%d, src_idx+offset%d);"
                    % (i, i, i))
    else:
        def get_copy_insn(i):
            return ("dest%d[dest_idx] = "
                    "fp_tex1Dfetch(tex_src%d, src_idx);" % (i, i))

    body = (("%(idx_tp)s src_idx = gmem_src_idx[gmem_src_idx_i];\n"
                "%(idx_tp)s dest_idx = gmem_dest_idx[gmem_dest_idx_i];\n" % ctx)
            + "\n".join(get_copy_insn(i) for i in range(vec_count)))

    mod = get_elwise_module(args, body, "take_put",
                            preamble=preamble, shape_arg_index=0)
    func = mod.get_function("take_put")
    tex_src = [mod.get_texref("tex_src%d" % i) for i in range(vec_count)]

    func.prepare(
            "PP"+(vec_count*"P")
            + (bool(with_offsets)*vec_count*idx_dtype.char)
            + np.dtype(np.uintp).char,
            texrefs=tex_src)
    return func, tex_src


@context_dependent_memoize
def get_put_kernel(dtype, idx_dtype, vec_count=1):
    ctx = {
            "idx_tp": dtype_to_ctype(idx_dtype),
            "tp": dtype_to_ctype(dtype),
            }

    args = [
            VectorArg(idx_dtype, "gmem_dest_idx"),
            ] + [
                VectorArg(dtype, "dest%d" % i)
                for i in range(vec_count)
            ] + [
                VectorArg(dtype, "src%d" % i)
                for i in range(vec_count)
            ] + [ScalarArg(np.intp, "n")]

    body = (
            "%(idx_tp)s dest_idx = gmem_dest_idx[gmem_dest_idx_i];\n" % ctx
            + "\n".join("dest%d[dest_idx] = src%d[src%d_i];" % (i, i, i)
                for i in range(vec_count)))

    func = get_elwise_module(args, body, "put",
                             shape_arg_index=0).get_function("put")
    func.prepare("P"+(2*vec_count*"P")+np.dtype(np.uintp).char)
    return func


@context_dependent_memoize
def get_copy_kernel(dtype_dest, dtype_src):
    return get_elwise_kernel(
            "%(tp_dest)s *dest, %(tp_src)s *src" % {
                "tp_dest": dtype_to_ctype(dtype_dest),
                "tp_src": dtype_to_ctype(dtype_src),
                },
            "dest[dest_i] = src[src_i]",
            "copy")


@context_dependent_memoize
def get_linear_combination_kernel(summand_descriptors,
        dtype_z):
    from pycuda.tools import dtype_to_ctype
    from pycuda.elementwise import \
            VectorArg, ScalarArg, get_elwise_module

    args = []
    preamble = ["#include <pycuda-helpers.hpp>\n\n"]
    loop_prep = []
    summands = []
    tex_names = []

    for i, (is_gpu_scalar, scalar_dtype, vector_dtype) in \
            enumerate(summand_descriptors):
        if is_gpu_scalar:
            preamble.append(
                    "texture <%s, 1, cudaReadModeElementType> tex_a%d;"
                    % (dtype_to_ctype(scalar_dtype, with_fp_tex_hack=True), i))
            args.append(VectorArg(vector_dtype, "x%d" % i))
            tex_names.append("tex_a%d" % i)
            loop_prep.append(
                    "%s a%d = fp_tex1Dfetch(tex_a%d, 0)"
                    % (dtype_to_ctype(scalar_dtype), i, i))
        else:
            args.append(ScalarArg(scalar_dtype, "a%d" % i))
            args.append(VectorArg(vector_dtype, "x%d" % i))

        summands.append("a%d*x%d[x%d_i]" % (i, i, i))

    args.append(VectorArg(dtype_z, "z"))
    args.append(ScalarArg(np.uintp, "n"))

    mod = get_elwise_module(args,
            "z[z_i] = " + " + ".join(summands),
            "linear_combination",
            preamble="\n".join(preamble),
            loop_prep=";\n".join(loop_prep))

    func = mod.get_function("linear_combination")
    tex_src = [mod.get_texref(tn) for tn in tex_names]
    func.prepare("".join(arg.struct_char for arg in args),
            texrefs=tex_src)

    return func, tex_src


@context_dependent_memoize
def get_axpbyz_kernel(dtype_x, dtype_y, dtype_z):
    return get_elwise_kernel(
            "%(tp_x)s a, %(tp_x)s *x, %(tp_y)s b, %(tp_y)s *y, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_y": dtype_to_ctype(dtype_y),
                "tp_z": dtype_to_ctype(dtype_z),
                },
            "z[z_i] = a*x[x_i] + b*y[y_i]",
            "axpbyz")


@context_dependent_memoize
def get_axpbz_kernel(dtype_x, dtype_z):
    return get_elwise_kernel(
            "%(tp_z)s a, %(tp_x)s *x,%(tp_z)s b, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_z": dtype_to_ctype(dtype_z)
                },
            "z[z_i] = a * x[x_i] + b",
            "axpb")


@context_dependent_memoize
def get_binary_op_kernel(dtype_x, dtype_y, dtype_z, operator):
    return get_elwise_kernel(
            "%(tp_x)s *x, %(tp_y)s *y, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_y": dtype_to_ctype(dtype_y),
                "tp_z": dtype_to_ctype(dtype_z),
                },
            "z[z_i] = x[x_i] %s y[y_i]" % operator,
            "binary_op")


@context_dependent_memoize
def get_rdivide_elwise_kernel(dtype_x, dtype_z):
    return get_elwise_kernel(
            "%(tp_x)s *x, %(tp_z)s y, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_z": dtype_to_ctype(dtype_z),
                },
            "z[z_i] = y / x[x_i]",
            "divide_r")


@context_dependent_memoize
def get_binary_func_kernel(func, dtype_x, dtype_y, dtype_z):
    return get_elwise_kernel(
            "%(tp_x)s *x, %(tp_y)s *y, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_y": dtype_to_ctype(dtype_y),
                "tp_z": dtype_to_ctype(dtype_z),
                },
            "z[z_i] = %s(x[x_i], y[y_i])" % func,
            func+"_kernel")


@context_dependent_memoize
def get_binary_func_scalar_kernel(func, dtype_x, dtype_y, dtype_z):
    return get_elwise_kernel(
            "%(tp_x)s *x, %(tp_y)s y, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_y": dtype_to_ctype(dtype_y),
                "tp_z": dtype_to_ctype(dtype_z),
                },
            "z[z_i] = %s(x[x_i], y)" % func,
            func+"_kernel")


def get_binary_minmax_kernel(func, dtype_x, dtype_y, dtype_z, use_scalar):
    if np.float64 not in [dtype_x, dtype_y]:
        func = func + "f"

    from pytools import any
    if any(dt.kind == "f" for dt in [dtype_x, dtype_y, dtype_z]):
        func = "f"+func

    if use_scalar:
        return get_binary_func_scalar_kernel(func, dtype_x, dtype_y, dtype_z)
    else:
        return get_binary_func_kernel(func, dtype_x, dtype_y, dtype_z)


@context_dependent_memoize
def get_fill_kernel(dtype):
    return get_elwise_kernel(
            "%(tp)s a, %(tp)s *z" % {
                "tp": dtype_to_ctype(dtype),
                },
            "z[z_i] = a",
            "fill")


@context_dependent_memoize
def get_reverse_kernel(dtype):
    return get_elwise_kernel(
            "%(tp)s *y, %(tp)s *z" % {
                "tp": dtype_to_ctype(dtype),
                },
            "z[z_i] = y[n-1-y_i]",
            "reverse")


@context_dependent_memoize
def get_real_kernel(dtype, real_dtype):
    return get_elwise_kernel(
            "%(tp)s *y, %(real_tp)s *z" % {
                "tp": dtype_to_ctype(dtype),
                "real_tp": dtype_to_ctype(real_dtype),
                },
            "z[z_i] = real(y[y_i])",
            "real")


@context_dependent_memoize
def get_imag_kernel(dtype, real_dtype):
    return get_elwise_kernel(
            "%(tp)s *y, %(real_tp)s *z" % {
                "tp": dtype_to_ctype(dtype),
                "real_tp": dtype_to_ctype(real_dtype),
                },
            "z[z_i] = imag(y[y_i])",
            "imag")


@context_dependent_memoize
def get_conj_kernel(dtype):
    return get_elwise_kernel(
            "%(tp)s *y, %(tp)s *z" % {
                "tp": dtype_to_ctype(dtype),
                },
            "z[z_i] = pycuda::conj(y[y_i])",
            "conj")


@context_dependent_memoize
def get_arange_kernel(dtype):
    return get_elwise_kernel(
            "%(tp)s *z, %(tp)s start, %(tp)s step" % {
                "tp": dtype_to_ctype(dtype),
                },
            "z[z_i] = start + ((%(tp)s)(z_i))*step" % {
                "tp": dtype_to_ctype(dtype),
                },
            "arange")


@context_dependent_memoize
def get_pow_kernel(dtype):
    if dtype == np.float32:
        func = "powf"
    else:
        func = "pow"

    return get_elwise_kernel(
            "%(tp)s value, %(tp)s *y, %(tp)s *z" % {
                "tp": dtype_to_ctype(dtype),
                },
            "z[z_i] = %s(y[y_i], value)" % func,
            "pow_method")


@context_dependent_memoize
def get_pow_array_kernel(dtype_x, dtype_y, dtype_z):
    if np.float64 in [dtype_x, dtype_y]:
        func = "pow"
    else:
        func = "powf"

    return get_elwise_kernel(
            "%(tp_x)s *x, %(tp_y)s *y, %(tp_z)s *z" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_y": dtype_to_ctype(dtype_y),
                "tp_z": dtype_to_ctype(dtype_z),
                },
            "z[z_i] = %s(x[x_i], y[y_i])" % func,
            "pow_method")


@context_dependent_memoize
def get_fmod_kernel():
    return get_elwise_kernel(
            "float *arg, float *mod, float *z",
            "z[z_i] = fmod(arg[arg_i], mod[mod_i])",
            "fmod_kernel")


@context_dependent_memoize
def get_modf_kernel():
    return get_elwise_kernel(
            "float *x, float *intpart ,float *fracpart",
            "fracpart[fracpart_i] = modf(x[x_i], &intpart[intpart_i])",
            "modf_kernel")


@context_dependent_memoize
def get_frexp_kernel():
    return get_elwise_kernel(
            "float *x, float *significand, float *exponent",
            """
                int expt = 0;
                significand[significand_i] = frexp(x[x_i], &expt);
                exponent[exponent_i] = expt;
            """,
            "frexp_kernel")


@context_dependent_memoize
def get_ldexp_kernel():
    return get_elwise_kernel(
            "float *sig, float *expt, float *z",
            "z[z_i] = ldexp(sig[sig_i], int(expt[expt_i]))",
            "ldexp_kernel")


@context_dependent_memoize
def get_unary_func_kernel(func_name, in_dtype, out_dtype=None):
    if out_dtype is None:
        out_dtype = in_dtype

    return get_elwise_kernel(
            "%(tp_in)s *y, %(tp_out)s *z" % {
                "tp_in": dtype_to_ctype(in_dtype),
                "tp_out": dtype_to_ctype(out_dtype),
                },
            "z[z_i] = %s(y[y_i])" % func_name,
            "%s_kernel" % func_name)


@context_dependent_memoize
def get_if_positive_kernel(crit_dtype, dtype):
    return get_elwise_kernel([
            VectorArg(crit_dtype, "crit"),
            VectorArg(dtype, "then_"),
            VectorArg(dtype, "else_"),
            VectorArg(dtype, "result"),
            ],
            "result[result_i] = crit[crit_i] > 0 ? then_[then__i] : else_[else__i]",
            "if_positive")


@context_dependent_memoize
def get_scalar_op_kernel(dtype_x, dtype_y, operator):
    return get_elwise_kernel(
            "%(tp_x)s *x, %(tp_a)s a, %(tp_y)s *y" % {
                "tp_x": dtype_to_ctype(dtype_x),
                "tp_y": dtype_to_ctype(dtype_y),
                "tp_a": dtype_to_ctype(dtype_x),
                },
            "y[y_i] = x[x_i] %s a" % operator,
            "scalarop_kernel")
