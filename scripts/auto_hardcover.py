from functools import reduce
import inspect
import json
import os
import requests
import sys

from cwa_db import AppDB, MetaDB

query_name = "HardcoverQuery"

class AutoHardcover:
    def __init__(self, debug):
        self.debug = debug
        self.hardcover_token = os.environ.get('HARDCOVER_TOKEN', None)
        self.hardcover_user_id = os.environ.get('HARDCOVER_USER_ID', None)

        if self.hardcover_token is None or self.hardcover_user_id is None:
            print("[cwa-auto-hardcover] Hardcover token and/or user ID environment variables missing")
            sys.exit(0)

        self.app_db = AppDB()
        self.meta_db = MetaDB()


    def log_if_debug(self, msg) -> None:
        if self.debug:
            print(msg)


    def run_db_select(self, db, query) -> list[dict]:
        db.cur.execute(query)
        headers = [header[0] for header in db.cur.description]
        return [dict(zip(headers, row)) for row in db.cur.fetchall()]


    def run_hardcover_query(self, query):
        return requests.post(
            "https://api.hardcover.app/v1/graphql",
            data = json.dumps({
                'query': query,
                'variables': {},
                'operationName': query_name
            }),
            headers = {
                'content-type': 'application/json',
                'authorization': f"Bearer {self.hardcover_token}"
            }
        ).json()


    def hardcover_read_book_ids_query(self) -> str:
        return inspect.cleandoc(f"""
            query {query_name} {{
                me {{
                    user_books(where: {{ status_id: {{ _eq: 3 }} }}) {{
                        book {{ id }}
                    }}
                }}
            }}
        """)


    def get_read_hardcover_book_ids(self) -> list[int]:
        print("[cwa-auto-hardcover] Fetching read books from Hardcover")
        res = self.run_hardcover_query(self.hardcover_read_book_ids_query())
        self.log_if_debug(f"[cwa-auto-hardcover] Hardcover read books response: {json.dumps(res, indent=2)}")

        return list(map(
            lambda b: b['book']['id'],
            res['data']['me'][0]['user_books']
        ))


    def hardcover_isbns_query(self, book_ids) -> str:
        return inspect.cleandoc(f"""
            query {query_name} {{
                editions(where: {{ book_id: {{ _in: {json.dumps(book_ids)} }} }}) {{
                    isbn_13
                }}
            }}
        """)


    def get_isbns_from_hardcover(self, book_ids) -> list[str]:
        print("[cwa-auto-hardcover] Fetching ISBNs from Hardcover")
        self.log_if_debug(f"[cwa-auto-hardcover] Hardcover book ids: {json.dumps(book_ids)}")
        res = self.run_hardcover_query(self.hardcover_isbns_query(book_ids))
        self.log_if_debug(f"[cwa-auto-hardcover] Hardcover read books response: {json.dumps(res, indent=2)}")

        return reduce(
            lambda acc, e: acc + [e['isbn_13']] if e['isbn_13'] is not None else acc,
            res['data']['editions'],
            []
        )


    def get_cwa_books_by_isbn(self, isbns) -> list[dict]:
        return self.run_db_select(self.meta_db, f"""
            SELECT b.id, b.title
            FROM books b
            JOIN identifiers i ON b.id = i.book AND i.type = 'isbn'
            WHERE i.val IN ({', '.join(['"' + i + '"' for i in isbns])})
        """)


    def get_already_read_cwa_books(self, book_ids) -> set[int]:
        if len(book_ids) == 0:
            return set()
        else:
            res = self.run_db_select(self.app_db, f"""
                SELECT book_id
                FROM book_read_link
                WHERE read_status = 1
                AND book_id IN ({', '.join(str(book_id) for book_id in book_ids)})
            """)
            return set([b['book_id'] for b in res])


    def sync_read_books(self) -> None:
        read_hardcover_book_ids = self.get_read_hardcover_book_ids()
        read_isbns = self.get_isbns_from_hardcover(read_hardcover_book_ids)
        cwa_books = self.get_cwa_books_by_isbn(read_isbns)
        already_read_cwa_books = self.get_already_read_cwa_books([b['id'] for b in cwa_books])
        unread_cwa_books = list(filter(lambda b: b['id'] not in already_read_cwa_books, cwa_books))

        for book in unread_cwa_books:
            print(f"[cwa-auto-hardcover] Marking book as read: {book}")
            self.app_db.cur.execute(f"""
                INSERT OR IGNORE INTO book_read_link (book_id, user_id, read_status, last_modified, times_started_reading) VALUES (
                    {book['id']},
                    1,
                    1,
                    datetime('now'),
                    0
                )
            """)

        self.app_db.con.commit()


    def get_books_without_ratings(self) -> dict:
        books_without_ratings = self.run_db_select(self.meta_db, """
            SELECT b.id, b.title, i.val AS isbn
            FROM books b
            JOIN identifiers i ON b.id = i.book AND i.type = 'isbn'
            LEFT JOIN books_ratings_link r ON b.id = r.book
            WHERE r.rating IS NULL
        """)
        return reduce(lambda acc, b: acc | { b['isbn']: { 'id': b['id'], 'title': b['title'] } }, books_without_ratings, {})


    def hardcover_ratings_query(self, isbns) -> str:
        return inspect.cleandoc(f"""
            query {query_name} {{
                editions(where: {{ isbn_13: {{ _in: {json.dumps(isbns)} }} }}) {{
                    isbn_13
                    book {{
                        user_books(where: {{ user_id: {{ _eq: {self.hardcover_user_id} }} }}) {{ rating }}
                    }}
                }}
            }}
        """)


    def get_ratings_from_hardcover(self, isbns) -> dict:
        if len(isbns) == 0:
            return []
        else:
            print(f"[cwa-auto-hardcover] Fetching Hardcover ratings")
            self.log_if_debug(f"[cwa-auto-hardcover] Hardcover ISBNs: {json.dumps(isbns)}")
            res = self.run_hardcover_query(self.hardcover_ratings_query(isbns))
            self.log_if_debug(f"[cwa-auto-hardcover] Hardcover ratings response: {json.dumps(res, indent=2)}")

            return reduce(
                lambda acc, e: acc | { e['isbn_13']: (e['book']['user_books'] or [{'rating': None}])[0]['rating'] },
                res['data']['editions'],
                {}
            )


    def sync_ratings(self) -> None:
        isbn_to_book = self.get_books_without_ratings()
        isbn_to_rating = self.get_ratings_from_hardcover(list(isbn_to_book.keys()))

        for isbn, book in isbn_to_book.items():
            rating = isbn_to_rating.get(isbn, None)
            if rating is not None:
                print(f"[cwa-auto-hardcover] Setting rating to {rating} for book: {book}")
                db_rating = int(float(rating) * 2)
                self.meta_db.cur.execute(f"INSERT OR IGNORE INTO ratings (rating) VALUES ({db_rating})")
                self.meta_db.cur.execute(f"""
                    INSERT INTO books_ratings_link (book, rating) VALUES (
                        {book['id']},
                        (SELECT id FROM ratings WHERE rating = {db_rating})
                    )
                """)

        self.meta_db.con.commit()


def main(debug):
    try:
        hardcover = AutoHardcover(debug)
        print("[cwa-auto-hardcover] Successfully initiated, syncing...")
    except Exception as e:
        print(f"[cwa-auto-hardcover] AutoHardcover could not be initiated due to the following error:\n{e}")
        sys.exit(1)
    try:
        hardcover.sync_read_books()
        hardcover.sync_ratings()
        print("[cwa-auto-hardcover] Successfully synced from Hardcover!")
    except Exception as e:
        print(f"[cwa-auto-hardcover] Failed to sync from Hardcover due to the following error:\n{e} ")
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1] == '--debug' if len(sys.argv) > 1 else False)
