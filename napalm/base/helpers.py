"""Helper functions for the NAPALM base."""

# Python3 support
from __future__ import print_function
from __future__ import unicode_literals

# std libs
import os
import sys
import itertools

# third party libs
import jinja2
import textfsm
from netaddr import EUI
from netaddr import mac_unix
from netaddr import IPAddress

# local modules
import napalm.base.exceptions
from napalm.base.utils.jinja_filters import CustomJinjaFilters
from napalm.base.utils import py23_compat
from napalm.base.canonical_map import base_interfaces, reverse_mapping


# ----------------------------------------------------------------------------------------------------------------------
# helper classes -- will not be exported
# ----------------------------------------------------------------------------------------------------------------------
class _MACFormat(mac_unix):
    pass


_MACFormat.word_fmt = '%.2X'


# ----------------------------------------------------------------------------------------------------------------------
# callable helpers
# ----------------------------------------------------------------------------------------------------------------------
def load_template(cls, template_name, template_source=None, template_path=None,
                  openconfig=False, jinja_filters={}, **template_vars):
    try:
        search_path = []
        if isinstance(template_source, py23_compat.string_types):
            template = jinja2.Template(template_source)
        else:
            if template_path is not None:
                if (isinstance(template_path, py23_compat.string_types) and
                        os.path.isdir(template_path) and os.path.isabs(template_path)):
                    # append driver name at the end of the custom path
                    search_path.append(os.path.join(template_path, cls.__module__.split('.')[-1]))
                else:
                    raise IOError("Template path does not exist: {}".format(template_path))
            else:
                # Search modules for template paths
                search_path = [os.path.dirname(os.path.abspath(sys.modules[c.__module__].__file__))
                               for c in cls.__class__.mro() if c is not object]

            if openconfig:
                search_path = ['{}/oc_templates'.format(s) for s in search_path]
            else:
                search_path = ['{}/templates'.format(s) for s in search_path]

            loader = jinja2.FileSystemLoader(search_path)
            environment = jinja2.Environment(loader=loader)

            for filter_name, filter_function in itertools.chain(
                    CustomJinjaFilters.filters().items(),
                    jinja_filters.items()):
                environment.filters[filter_name] = filter_function

            template = environment.get_template('{template_name}.j2'.format(
                template_name=template_name
            ))
        configuration = template.render(**template_vars)
    except jinja2.exceptions.TemplateNotFound:
        raise napalm.base.exceptions.TemplateNotImplemented(
            "Config template {template_name}.j2 not found in search path: {sp}".format(
                template_name=template_name,
                sp=search_path
            )
        )
    except (jinja2.exceptions.UndefinedError, jinja2.exceptions.TemplateSyntaxError) as jinjaerr:
        raise napalm.base.exceptions.TemplateRenderException(
            "Unable to render the Jinja config template {template_name}: {error}".format(
                template_name=template_name,
                error=py23_compat.text_type(jinjaerr),
            )
        )
    return cls.load_merge_candidate(config=configuration)


def textfsm_extractor(cls, template_name, raw_text):
    """
    Applies a TextFSM template over a raw text and return the matching table.

    Main usage of this method will be to extract data form a non-structured output
    from a network device and return the values in a table format.

    :param cls: Instance of the driver class
    :param template_name: Specifies the name of the template to be used
    :param raw_text: Text output as the devices prompts on the CLI
    :return: table-like list of entries
    """
    textfsm_data = list()
    fsm_handler = None
    for c in cls.__class__.mro():
        if c is object:
            continue
        current_dir = os.path.dirname(os.path.abspath(sys.modules[c.__module__].__file__))
        template_dir_path = '{current_dir}/utils/textfsm_templates'.format(
            current_dir=current_dir
        )
        template_path = '{template_dir_path}/{template_name}.tpl'.format(
            template_dir_path=template_dir_path,
            template_name=template_name
        )

        try:
            fsm_handler = textfsm.TextFSM(open(template_path))
        except IOError:  # Template not present in this class
            continue  # Continue up the MRO
        except textfsm.TextFSMTemplateError as tfte:
            raise napalm.base.exceptions.TemplateRenderException(
                "Wrong format of TextFSM template {template_name}: {error}".format(
                    template_name=template_name,
                    error=py23_compat.text_type(tfte)
                )
            )

        break  # Template found
    if not fsm_handler:
        raise napalm.base.exceptions.TemplateNotImplemented(
            "TextFSM template {template_name}.tpl is not defined under {path}".format(
                template_name=template_name,
                path=template_dir_path
            )
        )

    objects = fsm_handler.ParseText(raw_text)

    for obj in objects:
        index = 0
        entry = {}
        for entry_value in obj:
            entry[fsm_handler.header[index].lower()] = entry_value
            index += 1
        textfsm_data.append(entry)

    return textfsm_data


def find_txt(xml_tree, path, default=''):
    """
    Extracts the text value from an XML tree, using XPath.
    In case of error, will return a default value.

    :param xml_tree: the XML Tree object. Assumed is <type 'lxml.etree._Element'>.
    :param path:     XPath to be applied, in order to extract the desired data.
    :param default:  Value to be returned in case of error.
    :return: a str value.
    """
    value = ''
    try:
        xpath_applied = xml_tree.xpath(path)  # will consider the first match only
        if len(xpath_applied) and xpath_applied[0] is not None:
            xpath_result = xpath_applied[0]
            if isinstance(xpath_result, type(xml_tree)):
                value = xpath_result.text.strip()
            else:
                value = xpath_result
    except Exception:  # in case of any exception, returns default
        value = default
    return py23_compat.text_type(value)


def convert(to, who, default=u''):
    """
    Converts data to a specific datatype.
    In case of error, will return a default value.

    :param to:      datatype to be casted to.
    :param who:     value to cast.
    :param default: value to return in case of error.
    :return: a str value.
    """
    if who is None:
        return default
    try:
        return to(who)
    except:  # noqa
        return default


def mac(raw):
    """
    Converts a raw string to a standardised MAC Address EUI Format.

    :param raw: the raw string containing the value of the MAC Address
    :return: a string with the MAC Address in EUI format

    Example:

    .. code-block:: python

        >>> mac('0123.4567.89ab')
        u'01:23:45:67:89:AB'

    Some vendors like Cisco return MAC addresses like a9:c5:2e:7b:6: which is not entirely valid
    (with respect to EUI48 or EUI64 standards). Therefore we need to stuff with trailing zeros

    Example
    >>> mac('a9:c5:2e:7b:6:')
    u'A9:C5:2E:7B:60:00'

    If Cisco or other obscure vendors use their own standards, will throw an error and we can fix
    later, however, still works with weird formats like:

    >>> mac('123.4567.89ab')
    u'01:23:45:67:89:AB'
    >>> mac('23.4567.89ab')
    u'00:23:45:67:89:AB'
    """
    if raw.endswith(':'):
        flat_raw = raw.replace(':', '')
        raw = '{flat_raw}{zeros_stuffed}'.format(
            flat_raw=flat_raw,
            zeros_stuffed='0'*(12-len(flat_raw))
        )
    return py23_compat.text_type(EUI(raw, dialect=_MACFormat))


def ip(addr, version=None):
    """
    Converts a raw string to a valid IP address. Optional version argument will detect that \
    object matches specified version.

    Motivation: the groups of the IP addreses may contain leading zeros. IPv6 addresses can \
    contain sometimes uppercase characters. E.g.: 2001:0dB8:85a3:0000:0000:8A2e:0370:7334 has \
    the same logical value as 2001:db8:85a3::8a2e:370:7334. However, their values as strings are \
    not the same.

    :param raw: the raw string containing the value of the IP Address
    :param version: (optional) insist on a specific IP address version.
    :type version: int.
    :return: a string containing the IP Address in a standard format (no leading zeros, \
    zeros-grouping, lowercase)

    Example:

    .. code-block:: python

        >>> ip('2001:0dB8:85a3:0000:0000:8A2e:0370:7334')
        u'2001:db8:85a3::8a2e:370:7334'
    """
    addr_obj = IPAddress(addr)
    if version and addr_obj.version != version:
        raise ValueError("{} is not an ipv{} address".format(addr, version))
    return py23_compat.text_type(addr_obj)


def as_number(as_number_val):
    """Convert AS Number to standardized asplain notation as an integer."""
    as_number_str = py23_compat.text_type(as_number_val)
    if '.' in as_number_str:
        big, little = as_number_str.split('.')
        return (int(big) << 16) + int(little)
    else:
        return int(as_number_str)


def split_interface(intf_name):
    """Split an interface name based on first digit, slash, or space match."""
    head = intf_name.rstrip(r'/\0123456789. ')
    tail = intf_name[len(head):].lstrip()
    return (head, tail)


def canonical_interface_name(interface, addl_name_map=None):
    """Function to return an interface's canonical name (fully expanded name).

    Use of explicit matches used to indicate a clear understanding on any potential
    match. Regex and other looser matching methods were not implmented to avoid false
    positive matches. As an example, it would make sense to do "[P|p][O|o]" which would
    incorrectly match PO = POS and Po = Port-channel, leading to a false positive, not
    easily troubleshot, found, or known.

    :param interface: The interface you are attempting to expand.
    :param addl_name_map (optional): A dict containing key/value pairs that updates
    the base mapping. Used if an OS has specific differences. e.g. {"Po": "PortChannel"} vs
    {"Po": "Port-Channel"}
    """

    name_map = {}
    name_map.update(base_interfaces)
    interface_type, interface_number = split_interface(interface)

    if isinstance(addl_name_map, dict):
        name_map.update(addl_name_map)
    # check in dict for mapping
    if name_map.get(interface_type):
        long_int = name_map.get(interface_type)
        return long_int + py23_compat.text_type(interface_number)
    # if nothing matched, return the original name
    else:
        return interface


def abbreviated_interface_name(interface, addl_name_map=None, addl_reverse_map=None):
    """Function to return an abbreviated representation of the interface name.

    :param interface: The interface you are attempting to abbreviate.
    :param addl_name_map (optional): A dict containing key/value pairs that updates
    the base mapping. Used if an OS has specific differences. e.g. {"Po": "PortChannel"} vs
    {"Po": "Port-Channel"}
    :param addl_reverse_map (optional): A dict containing key/value pairs that updates
    the reverse mapping. Used if an OS has specific differences. e.g. {"PortChannel": "Po"} vs
    {"PortChannel": "po"}
    """

    name_map = {}
    name_map.update(base_interfaces)
    interface_type, interface_number = split_interface(interface)

    if isinstance(addl_name_map, dict):
        name_map.update(addl_name_map)

    rev_name_map = {}
    rev_name_map.update(reverse_mapping)

    if isinstance(addl_reverse_map, dict):
        rev_name_map.update(addl_reverse_map)

    # Try to ensure canonical type.
    if name_map.get(interface_type):
        canonical_type = name_map.get(interface_type)
    else:
        canonical_type = interface_type

    try:
        abbreviated_name = rev_name_map[canonical_type] + py23_compat.text_type(interface_number)
        return abbreviated_name
    except KeyError:
        pass

    # If abbreviated name lookup fails, return original name
    return interface
