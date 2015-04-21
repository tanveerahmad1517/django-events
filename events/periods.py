from __future__ import unicode_literals
from six.moves.builtins import range
from six.moves.builtins import object
import pytz
import datetime
import calendar as standardlib_calendar

from django.conf import settings
from django.utils.encoding import python_2_unicode_compatible
from django.template.defaultfilters import date as date_filter
from django.utils.dates import WEEKDAYS, WEEKDAYS_ABBR
from events.settings import FIRST_DAY_OF_WEEK, SHOW_CANCELLED_OCCURRENCES
from events.models import Occurrence
from django.utils import timezone

weekday_names = []
weekday_abbrs = []
if FIRST_DAY_OF_WEEK == 1:
    # The calendar week starts on Monday
    for i in range(7):
        weekday_names.append(WEEKDAYS[i])
        weekday_abbrs.append(WEEKDAYS_ABBR[i])
else:
    # The calendar week starts on Sunday, not Monday
    weekday_names.append(WEEKDAYS[6])
    weekday_abbrs.append(WEEKDAYS_ABBR[6])
    for i in range(6):
        weekday_names.append(WEEKDAYS[i])
        weekday_abbrs.append(WEEKDAYS_ABBR[i])


class Period(object):
    '''
    This class represents a period of time. It can return a set of occurrences
    based on its events, and its time period (start and end).
    '''
    def __init__(self, events, start, end, parent_persisted_occurrences=None,
                 occurrence_pool=None, tzinfo=pytz.utc):
        self.utc_start = self._normalize_timezone_to_utc(start, tzinfo) or datetime.datetime.now(tzinfo)
        self.utc_end = self._normalize_timezone_to_utc(end, tzinfo) or self.utc_start + datetime.timedelta(days=30)

        self.events = events
        self.tzinfo = self._get_tzinfo(tzinfo)
        self.occurrence_pool = occurrence_pool
        if parent_persisted_occurrences is not None:
            self._persisted_occurrences = parent_persisted_occurrences

    def _normalize_timezone_to_utc(self, point_in_time, tzinfo):
        if point_in_time.tzinfo is not None:
            return point_in_time.astimezone(pytz.utc)
        if tzinfo is not None:
            return tzinfo.localize(point_in_time).astimezone(pytz.utc)
        if settings.USE_TZ:
            return pytz.utc.localize(point_in_time)
        else:
            if timezone.is_aware(point_in_time):
                return timezone.make_naive(point_in_time, pytz.utc)
            else:
                return point_in_time

    def __eq__(self, period):
        return self.utc_start == period.utc_start and self.utc_end == period.utc_end and self.events == period.events

    def __ne__(self, period):
        return self.utc_start != period.start or self.utc_end != period.utc_end or self.events != period.events

    def _get_tzinfo(self, tzinfo):
        return tzinfo if settings.USE_TZ else None

    def _get_sorted_occurrences(self):
        occurrences = []
        if hasattr(self, "occurrence_pool") and self.occurrence_pool is not None:
            for occurrence in self.occurrence_pool:
                if occurrence.start < self.utc_end and occurrence.end > self.utc_start:
                    occurrences.append(occurrence)
            return occurrences

        if hasattr(self.events, 'prefetch_related'):
            self.events = self.events.select_related('calendar').prefetch_related('rule', 'occurrence_set')
        for event in self.events:
            event_occurrences = event.get_occurrences(self.utc_start, self.utc_end)
            occurrences += event_occurrences
        return sorted(occurrences)

    def cached_get_sorted_occurrences(self):
        if hasattr(self, '_occurrences'):
            return self._occurrences
        occs = self._get_sorted_occurrences()
        self._occurrences = occs
        return occs
    occurrences = property(cached_get_sorted_occurrences)

    def get_all_day_occurrences(self):
        occ_list = [o for o in self.occurrences if o.event.all_day is True and o.end < self.utc_end]
        return occ_list

    def get_persisted_occurrences(self):
        if hasattr(self, '_persisted_occurrenes'):
            return self._persisted_occurrences
        else:
            self._persisted_occurrences = Occurrence.objects.filter(event__in=self.events)
            return self._persisted_occurrences

    def classify_occurrence(self, occurrence):
        """
        You use this method to determine how the occurrence relates to the
        period. This method returns a dictionary. The keys are ``class``,
        ``occurrence`` and ``all_day``. The ``all_day`` key is a boolean that
        is ``True`` if the occurrence is an all day event and is ``False`` if
        the occurrence is not an all day event.  The ``class`` key returns a
        number from 0 to 3 and the occurrence key returns the occurrence.

        Classes:

            | 0 - Only started during this period.
            | 1 - Started and ended during this period.
            | 2 - Didn't start or end in this period, but does exist during this period.
            | 3 - Only ended during this period.
        """

        if occurrence.cancelled and not SHOW_CANCELLED_OCCURRENCES:
            return
        if occurrence.start > self.utc_end or occurrence.end < self.utc_start:
            return

        all_day = False
        started = False
        ended = False
        rtn_dict = {
            'occurrence': occurrence,
            'class': 2,
            'all_day': all_day,
        }

        if occurrence.event.all_day is True:
            all_day = True
            rtn_dict['all_day'] = True

        if self.utc_start <= occurrence.start < self.utc_end:
            started = True
        if self.utc_start <= occurrence.end < self.utc_end:
            ended = True
        if started and ended:
            rtn_dict['class'] = 1
        elif started:
            rtn_dict['class'] = 0
        elif ended:
            rtn_dict['class'] = 3
        # it existed during this period but it didn't begin or end within it
        # so it must have just continued
        return rtn_dict

    def get_occurrence_partials(self):
        occurrence_dicts = []
        for occurrence in self.occurrences:
            occurrence = self.classify_occurrence(occurrence)
            if occurrence:
                occurrence_dicts.append(occurrence)
        return occurrence_dicts

    def get_occurrences(self):
        return self.occurrences

    def has_occurrences(self):
        return any(self.classify_occurrence(o) for o in self.occurrences)

    def get_time_slot(self, start, end, occurrence_pool=None):
        if start >= self.utc_start and end <= self.utc_end:
            return Period(self.events, start, end,
                parent_persisted_occurrences=self.get_persisted_occurrences(),
                occurrence_pool=self.occurrences)
        return None

    def create_sub_period(self, cls, start=None, occurrence_pool=None, tzinfo=None):
        if tzinfo is None:
            tzinfo = self.tzinfo
        start = start or self.utc_start
        occurrences = occurrence_pool or self.occurrences
        return cls(self.events, start, self.get_persisted_occurrences(), occurrences, tzinfo)

    def get_periods(self, cls, occurrence_pool=None, tzinfo=None):
        if tzinfo is None:
            tzinfo = self.tzinfo
        period = self.create_sub_period(cls, occurrence_pool=occurrence_pool, tzinfo=tzinfo)
        while period.start < self.utc_end:
            yield self.create_sub_period(cls, period.start, occurrence_pool=occurrence_pool, tzinfo=tzinfo)
            period = next(period)

    @property
    def start(self):
        if self.tzinfo is not None:
            return self.utc_start.astimezone(self.tzinfo)
        return self.utc_start.replace(tzinfo=None)

    @property
    def end(self):
        if self.tzinfo is not None:
            return self.utc_end.astimezone(self.tzinfo)
        return self.utc_end.replace(tzinfo=None)


@python_2_unicode_compatible
class Year(Period):
    def __init__(self, events, date=None, parent_persisted_occurrences=None, tzinfo=pytz.utc):
        if date is None:
            date = timezone.now()
        start, end = self._get_year_range(date)
        super(Year, self).__init__(events, start, end, parent_persisted_occurrences, tzinfo=tzinfo)

    def get_months(self):
        return self.get_periods(Month)

    def next_year(self):
        return Year(self.events, self.utc_end)
    next = __next__ = next_year

    def prev_year(self):
        start = datetime.datetime(self.start.year - 1, self.start.month, self.start.day)
        return Year(self.events, start, tzinfo=self.tzinfo)
    prev = prev_year

    def _get_year_range(self, year):
        # If tzinfo is not none get the local start of the year and convert it to utc.
        naive_start = datetime.datetime(year.year, datetime.datetime.min.month, datetime.datetime.min.day)
        naive_end = datetime.datetime(year.year + 1, datetime.datetime.min.month, datetime.datetime.min.day)

        start = naive_start
        end = naive_end
        if self.tzinfo is not None:
            local_start = self.tzinfo.localize(naive_start)
            local_end = self.tzinfo.localize(naive_end)
            start = local_start.astimezone(pytz.utc)
            end = local_end.astimezone(pytz.utc)

        return start, end

    def __str__(self):
        return self.start.year


@python_2_unicode_compatible
class Month(Period):
    """
    The month period has functions for retrieving the week periods within this period
    and day periods within the date.
    """
    def __init__(self, events, date=None, parent_persisted_occurrences=None,
                 occurrence_pool=None, tzinfo=pytz.utc):
        self.tzinfo = self._get_tzinfo(tzinfo)
        if date is None:
            date = timezone.now()
        start, end = self._get_month_range(date)
        super(Month, self).__init__(events, start, end,
                                    parent_persisted_occurrences, occurrence_pool, tzinfo=tzinfo)

    def get_weeks(self):
        return self.get_periods(Week)

    def get_days(self):
        return self.get_periods(Day)

    def get_day(self, daynumber):
        date = self.utc_start
        if daynumber > 1:
            date += datetime.timedelta(days=daynumber - 1)
        return self.create_sub_period(Day, date)

    def next_month(self):
        return Month(self.events, self.end, tzinfo=self.tzinfo)
    next = __next__ = next_month

    def prev_month(self):
        start = (self.start - datetime.timedelta(days=1)).replace(day=1, tzinfo=self.tzinfo)
        return Month(self.events, start, tzinfo=self.tzinfo)
    prev = prev_month

    def current_year(self):
        return Year(self.events, self.start, tzinfo=self.tzinfo)

    def prev_year(self):
        start = datetime.datetime.min.replace(year=self.start.year - 1, tzinfo=self.tzinfo)
        return Year(self.events, start, tzinfo=self.tzinfo)

    def next_year(self):
        start = datetime.datetime.min.replace(year=self.start.year + 1, tzinfo=self.tzinfo)
        return Year(self.events, start, tzinfo=self.tzinfo)

    def _get_month_range(self, month):
        year = month.year
        month = month.month
        # If tzinfo is not none get the local start of the month and convert it to utc.
        naive_start = datetime.datetime.min.replace(year=year, month=month)
        if month == 12:
            naive_end = datetime.datetime.min.replace(month=1, year=year + 1, day=1)
        else:
            naive_end = datetime.datetime.min.replace(month=month + 1, year=year, day=1)

        start = naive_start
        end = naive_end
        if self.tzinfo is not None:
            local_start = self.tzinfo.localize(naive_start)
            local_end = self.tzinfo.localize(naive_end)
            start = local_start.astimezone(pytz.utc)
            end = local_end.astimezone(pytz.utc)

        return start, end

    def __str__(self):
        return "%s %s" % (self.name(), self.year())

    def name(self):
        return standardlib_calendar.month_name[self.start.month]

    def year(self):
        return self.start.year


@python_2_unicode_compatible
class Week(Period):
    """
    The Week period that has functions for retrieving Day periods within it
    """

    def __init__(self, events, date=None, parent_persisted_occurrences=None,
                 occurrence_pool=None, tzinfo=pytz.utc):
        self.tzinfo = self._get_tzinfo(tzinfo)
        if date is None:
            date = timezone.now()
        start, end = self._get_week_range(date)
        super(Week, self).__init__(events, start, end,
                                   parent_persisted_occurrences, occurrence_pool, tzinfo=tzinfo)

    def prev_week(self):
        return Week(self.events, self.start - datetime.timedelta(days=7), tzinfo=self.tzinfo)
    prev = prev_week

    def next_week(self):
        return Week(self.events, self.end, tzinfo=self.tzinfo)
    next = __next__ = next_week

    def current_month(self):
        return Month(self.events, self.start, tzinfo=self.tzinfo)

    def current_year(self):
        return Year(self.events, self.start, tzinfo=self.tzinfo)

    def get_days(self):
        return self.get_periods(Day)

    def _get_week_range(self, week):
        if isinstance(week, datetime.datetime):
            week = week.date()
        # Adjust the start datetime to midnight of the week datetime
        naive_start = datetime.datetime.combine(week, datetime.time.min)
        # Adjust the start datetime to Monday or Sunday of the current week
        sub_days = 0
        if FIRST_DAY_OF_WEEK == 1:
            # The week begins on Monday
            sub_days = naive_start.isoweekday() - 1
        else:
            # The week begins on Sunday
            sub_days = naive_start.isoweekday()
            if sub_days == 7:
                sub_days = 0
        if sub_days > 0:
            naive_start = naive_start - datetime.timedelta(days=sub_days)
        naive_end = naive_start + datetime.timedelta(days=7)

        if self.tzinfo is not None:
            local_start = self.tzinfo.localize(naive_start)
            local_end = self.tzinfo.localize(naive_end)
            start = local_start.astimezone(pytz.utc)
            end = local_end.astimezone(pytz.utc)
        else:
            start = naive_start
            end = naive_end

        return start, end

    def __str__(self):
        date_format = 'l, %s' % settings.DATE_FORMAT
        return 'Week: %(start)s-%(end)s' % {
            'start': date_filter(self.start, date_format),
            'end': date_filter(self.end, date_format),
        }


@python_2_unicode_compatible
class Day(Period):
    def __init__(self, events, date=None, parent_persisted_occurrences=None,
                 occurrence_pool=None, tzinfo=pytz.utc):
        self.tzinfo = self._get_tzinfo(tzinfo)
        if date is None:
            date = timezone.now()
        start, end = self._get_day_range(date)
        super(Day, self).__init__(events, start, end,
                                  parent_persisted_occurrences, occurrence_pool, tzinfo=tzinfo)

    def _get_day_range(self, date):
        if isinstance(date, datetime.datetime):
            date = date.date()

        naive_start = datetime.datetime.combine(date, datetime.time.min)
        naive_end = datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time.min)
        if self.tzinfo is not None:
            local_start = self.tzinfo.localize(naive_start)
            local_end = self.tzinfo.localize(naive_end)
            start = local_start.astimezone(pytz.utc)
            end = local_end.astimezone(pytz.utc)
        else:
            start = naive_start
            end = naive_end

        return start, end

    def is_today(self):
        if self.utc_start.date() == datetime.date.today():
            return True
        else:
            return False

    def __str__(self):
        from django.template.defaultfilters import date
        return date(self.utc_start, "l, %s" % settings.DATE_FORMAT)

    def is_past(self):
        date = datetime.datetime.now()
        start = datetime.datetime.combine(date.date(), datetime.time.min)
        return self.utc_start < start

    def prev_day(self):
        return Day(self.events, self.start - datetime.timedelta(days=1), tzinfo=self.tzinfo)
    prev = prev_day

    def next_day(self):
        return Day(self.events, self.end, tzinfo=self.tzinfo)
    next = __next__ = next_day

    def current_year(self):
        return Year(self.events, self.start, tzinfo=self.tzinfo)

    def current_month(self):
        return Month(self.events, self.start, tzinfo=self.tzinfo)

    def current_week(self):
        return Week(self.events, self.start, tzinfo=self.tzinfo)
