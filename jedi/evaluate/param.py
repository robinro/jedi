import copy
from itertools import chain

from jedi._compatibility import unicode, zip_longest
from jedi.parser import representation as pr
from jedi.evaluate import iterable
from jedi import common
from jedi.evaluate import helpers
from jedi.evaluate import analysis


class ExecutedParam(pr.Param):
    def __init__(self):
        """Don't use this method, it's just here to overwrite the old one."""
        pass

    @classmethod
    def from_param(cls, param, parent, var_args):
        instance = cls()
        before = ()
        for cls in param.__class__.__mro__:
            with common.ignored(AttributeError):
                if before == cls.__slots__:
                    continue
                before = cls.__slots__
                for name in before:
                    setattr(instance, name, getattr(param, name))

        instance.original_param = param
        instance.is_generated = True
        instance.parent = parent
        instance.var_args = var_args
        return instance


def _get_calling_var_args(evaluator, var_args):
    old_var_args = None
    while var_args != old_var_args:
        old_var_args = var_args
        for argument in reversed(var_args):
            if not isinstance(argument, pr.Statement):
                continue
            exp_list = argument.expression_list()
            if len(exp_list) != 2 or exp_list[0] not in ('*', '**'):
                continue

            names, _ = evaluator.goto(argument, [exp_list[1].get_code()])
            if len(names) != 1:
                break
            param = names[0].parent
            if not isinstance(param, ExecutedParam):
                if isinstance(param, pr.Param):
                    # There is no calling var_args in this case - there's just
                    # a param without any input.
                    return None
                break
            # We never want var_args to be a tuple. This should be enough for
            # now, we can change it later, if we need to.
            if isinstance(param.var_args, pr.Array):
                var_args = param.var_args
    return var_args


def get_params(evaluator, func, var_args):
    result = []
    start_offset = 0
    from jedi.evaluate.representation import InstanceElement
    if isinstance(func, InstanceElement):
        # Care for self -> just exclude it and add the instance
        start_offset = 1
        self_name = copy.copy(func.params[0].get_name())
        self_name.parent = func.instance
        result.append(self_name)

    param_dict = {}
    for param in func.params:
        param_dict[str(param.get_name())] = param
    # There may be calls, which don't fit all the params, this just ignores it.
    var_arg_iterator = common.PushBackIterator(_var_args_iterator(evaluator, var_args))

    non_matching_keys = []
    keys_used = set()
    keys_only = False
    va_values = None
    for param in func.params[start_offset:]:
        # The value and key can both be null. There, the defaults apply.
        # args / kwargs will just be empty arrays / dicts, respectively.
        # Wrong value count is just ignored. If you try to test cases that are
        # not allowed in Python, Jedi will maybe not show any completions.
        key, va_values = next(var_arg_iterator, (None, []))
        while key:
            keys_only = True
            try:
                key_param = param_dict[unicode(key)]
            except KeyError:
                non_matching_keys.append((key, va_values))
            else:
                k = unicode(key)
                if k in keys_used:
                    m = ("TypeError: %s() got multiple values for keyword argument '%s'."
                         % (func.name, k))
                    analysis.add(evaluator, 'type-error-multiple-values',
                                 var_args, message=m)
                else:
                    keys_used.add(k)
                    result.append(_gen_param_name_copy(func, var_args, key_param,
                                                       values=va_values))
            key, va_values = next(var_arg_iterator, (None, []))

        keys = []
        values = []
        array_type = None
        has_default_value = False
        if param.stars == 1:
            # *args param
            array_type = pr.Array.TUPLE
            values += va_values
            for key, va_values in var_arg_iterator:
                # Iterate until a key argument is found.
                if key:
                    var_arg_iterator.push_back((key, va_values))
                    break
                values += va_values
        elif param.stars == 2:
            # **kwargs param
            array_type = pr.Array.DICT
            if non_matching_keys:
                keys, values = zip(*non_matching_keys)
                values = list(chain(*values))
            non_matching_keys = []
        else:
            # normal param
            if va_values:
                values = va_values
            else:
                if param.assignment_details:
                    # No value: return the default values.
                    has_default_value = True
                    result.append(param.get_name())
                    # TODO is this allowed? it changes it long time.
                    param.is_generated = True
                else:
                    # If there is no assignment detail, that means there is no
                    # assignment, just the result. Therefore nothing has to be
                    # returned.
                    values = []
                    if not keys_only and isinstance(var_args, pr.Array):
                        calling_va = _get_calling_var_args(evaluator, var_args)
                        if calling_va is not None:
                            m = _error_argument_count(func, len(var_args))
                            analysis.add(evaluator, 'type-error-too-few-arguments',
                                         calling_va, message=m)

        # Now add to result if it's not one of the previously covered cases.
        if not has_default_value and (not keys_only or param.stars == 2):
            keys_used.add(unicode(param.get_name()))
            result.append(_gen_param_name_copy(func, var_args, param,
                                               keys=keys, values=values,
                                               array_type=array_type))

    if keys_only:
        # All arguments should be handed over to the next function. It's not
        # about the values inside, it's about the names. Jedi needs to now that
        # there's nothing to find for certain names.
        for k in set(param_dict) - keys_used:
            result.append(_gen_param_name_copy(func, var_args, param_dict[k]))

    for key, va_values in non_matching_keys:
        m = "TypeError: %s() got an unexpected keyword argument '%s'." \
            % (func.name, key)
        for value in va_values:
            analysis.add(evaluator, 'type-error-keyword-argument', value, message=m)

    remaining_params = list(var_arg_iterator)
    if remaining_params:
        m = _error_argument_count(func, len(func.params) + len(remaining_params))
        for p in remaining_params[0][1]:
            analysis.add(evaluator, 'type-error-too-many-arguments',
                         p, message=m)
    return result


def _var_args_iterator(evaluator, var_args):
    """
    Yields a key/value pair, the key is None, if its not a named arg.
    """
    # `var_args` is typically an Array, and not a list.
    for stmt in var_args:
        if not isinstance(stmt, pr.Statement):
            if stmt is None:
                yield None, []
                continue
            old = stmt
            # generate a statement if it's not already one.
            stmt = helpers.FakeStatement([old])

        expression_list = stmt.expression_list()
        if not len(expression_list):
            continue
        # *args
        if expression_list[0] == '*':
            # *args must be some sort of an array, otherwise -> ignore
            arrays = evaluator.eval_expression_list(expression_list[1:])
            iterators = [_iterate_star_args(a) for a in arrays]
            for values in zip_longest(*iterators):
                yield None, [v for v in values if v is not None]
        # **kwargs
        elif expression_list[0] == '**':
            for array in evaluator.eval_expression_list(expression_list[1:]):
                if isinstance(array, iterable.Array):
                    for key_stmt, value_stmt in array.items():
                        # first index, is the key if syntactically correct
                        call = key_stmt.expression_list()[0]
                        if isinstance(call, pr.Name):
                            yield call, [value_stmt]
                        elif isinstance(call, pr.Call):
                            yield call.name, [value_stmt]
        # Normal arguments (including key arguments).
        else:
            if stmt.assignment_details:
                key_arr, op = stmt.assignment_details[0]
                # named parameter
                if key_arr and isinstance(key_arr[0], pr.Call):
                    yield key_arr[0].name, [stmt]
            else:
                yield None, [stmt]


def _iterate_star_args(array):
    if isinstance(array, iterable.Array):
        for field_stmt in array:  # yield from plz!
            yield field_stmt
    elif isinstance(array, iterable.Generator):
        for field_stmt in array.iter_content():
            yield helpers.FakeStatement([field_stmt])
    else:
        pass  # TODO need a warning here.


def _gen_param_name_copy(func, var_args, param, keys=(), values=(), array_type=None):
    """
    Create a param with the original scope (of varargs) as parent.
    """
    if isinstance(var_args, pr.Array):
        parent = var_args.parent
        start_pos = var_args.start_pos
    else:
        parent = func
        start_pos = 0, 0

    new_param = ExecutedParam.from_param(param, parent, var_args)

    # create an Array (-> needed for *args/**kwargs tuples/dicts)
    arr = pr.Array(helpers.FakeSubModule, start_pos, array_type, parent)
    arr.values = values
    key_stmts = []
    for key in keys:
        key_stmts.append(helpers.FakeStatement([key], start_pos))
    arr.keys = key_stmts
    arr.type = array_type

    new_param.set_expression_list([arr])

    name = copy.copy(param.get_name())
    name.parent = new_param
    return name


def _error_argument_count(func, actual_count):
    return ('TypeError: %s() takes exactly %s arguments (%s given).'
            % (func.name, len(func.params), actual_count))
