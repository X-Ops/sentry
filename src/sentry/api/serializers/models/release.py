from __future__ import absolute_import

import six
from collections import Counter, defaultdict
# from sentry import features
# from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from sentry.api.serializers import Serializer, register, serialize
from sentry.models import Release, ReleaseCommit, ReleaseProject, TagValue, User


@register(Release)
class ReleaseSerializer(Serializer):
    def _get_commit_metadata(self, item_list, user):
        release_commits = list(ReleaseCommit.objects.filter(
            release__in=item_list).prefetch_related("commit__author"))

        commit_count_by_release_id = Counter()
        authors_by_release_id = defaultdict(dict)

        author_emails = set(rc.commit.author.email for rc in release_commits)

        # TODO: Consider UserEmail models, organization filter
        # NOTE: Possible to return multiple User objects for a single email
        users = list(User.objects.filter(email__in=author_emails))
        users_by_email = {}
        for user in users:
            # Duplicates will clobber existing record in dict
            users_by_email[user.email] = serialize(user)

        for rc in release_commits:
            # Count commits per release
            commit_count_by_release_id[rc.release_id] += 1

            # Accumulate authors per release
            release_authors = authors_by_release_id[rc.release_id]
            if rc.commit.author_id not in release_authors:
                author = rc.commit.author
                if author.email in users_by_email:
                    # Author has a matching Sentry user
                    release_authors[author.id] = users_by_email[author.email]
                else:
                    release_authors[author.id] = {
                        "name": author.name,
                        "email": author.email
                    }

        result = {}
        for item in item_list:
            result[item] = {
                'commit_count': commit_count_by_release_id[item.id],
                'author_count': 0,
                'authors': authors_by_release_id[item.id].values(),
            }
        return result

    def get_attrs(self, item_list, user):
        tags = {
            tk.value: tk
            for tk in TagValue.objects.filter(
                project_id__in=ReleaseProject.objects.filter(
                    release__in=item_list
                ).values_list('project_id', flat=True),
                key='sentry:release',
                value__in=[o.version for o in item_list],
            )
        }
        owners = {
            d['id']: d
            for d in serialize(set(i.owner for i in item_list if i.owner_id), user)
        }

        release_metadata_attrs = self._get_commit_metadata(item_list, user)

        result = {}
        for item in item_list:
            result[item] = {
                'tag': tags.get(item.version),
                'owner': owners[six.text_type(item.owner_id)] if item.owner_id else None,
            }
            result[item].update(release_metadata_attrs[item])

        return result

    def serialize(self, obj, attrs, user):
        d = {
            'version': obj.version,
            'shortVersion': obj.short_version,
            'ref': obj.ref,
            'url': obj.url,
            'dateStarted': obj.date_started,
            'dateReleased': obj.date_released,
            'dateCreated': obj.date_added,
            'data': obj.data,
            'newGroups': obj.new_groups,
            'owner': attrs['owner'],
            'commitCount': attrs['commit_count'],
            'authorCount': attrs['author_count'],
            'authors': attrs['authors'],
        }
        if attrs['tag']:
            d.update({
                'lastEvent': attrs['tag'].last_seen,
                'firstEvent': attrs['tag'].first_seen,
            })
        else:
            d.update({
                'lastEvent': None,
                'firstEvent': None,
            })
        return d
