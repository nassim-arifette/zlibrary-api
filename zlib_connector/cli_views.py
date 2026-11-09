"""Render catalog and account commands without owning authentication or input."""
import json
import sys

from .eapi import Client
from .search import SearchFilters


def show_books(books, *, file=None):
    file = sys.stdout if file is None else file
    if not books:
        print("No books found.", file=file)
    for index, book in enumerate(books, 1):
        reference = f" · {book.book_id}:{book.book_hash}" if book.book_id and book.book_hash else ""
        saved = " [saved]" if book.is_favorite else ""
        print(f"{index:2}. {book.title}{saved}\n    {book.authors} · {book.year} · {book.language} · {book.extension.upper() or '?'} · {book.size}{reference}", file=file)



def show_search(client: Client, args, filters: SearchFilters):
    options = {"filters": filters, "page": args.page, "limit": args.limit}
    if args.with_pagination:
        result = client.search_page(args.query, **options)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False))
        else:
            show_books(result.books)
            print(f"Page {result.page} · total: {result.total_items if result.total_items is not None else 'unknown'}")
    else:
        books = client.search(args.query, **options)
        if args.json:
            print(json.dumps([book.to_dict() for book in books], ensure_ascii=False))
        else:
            show_books(books)



def show_book_command(client: Client, args):
    if args.command == "info":
        book = client.get_book(args.reference)
        if args.json:
            print(json.dumps(book.to_dict(), ensure_ascii=False))
        else:
            show_books([book])
            print(f"Reference: {book.book_id}:{book.book_hash}")
            for label, value in (("Publisher", book.publisher), ("Edition", book.edition),
                                 ("Pages", book.pages), ("ISBN-10", book.isbn10), ("ISBN-13", book.isbn13),
                                 ("Categories", ", ".join(book.categories)), ("Cover", book.cover_url),
                                 ("Description", book.description)):
                if value:
                    print(f"{label}: {value}")
    else:
        formats = client.get_formats(args.reference)
        if args.json:
            print(json.dumps([item.to_dict() for item in formats], ensure_ascii=False))
        elif not formats:
            print("The server reported no alternative formats.")
        else:
            for item in formats:
                support = "" if item.to_dict()["download_supported"] else " (download unsupported)"
                print(f"{item.book_id}:{item.book_hash} · {item.extension.upper()} · {item.size}{support}")



def run_catalog_command(client: Client, args, filters: SearchFilters | None):
    if args.command == "search":
        show_search(client, args, filters)
        return
    if args.command in ("info", "formats"):
        show_book_command(client, args)
        return
    if args.command == "similar":
        books = client.get_similar(args.reference)
    elif args.command == "popular":
        books = client.get_popular()
    else:
        books = client.get_recommendations()
    books = books[:args.limit]
    if args.json:
        print(json.dumps([book.to_dict() for book in books], ensure_ascii=False))
    else:
        show_books(books)



def run_library_command(client: Client, args):
    def show_page(page):
        show_books(page.books)
        total = page.total_items if page.total_items is not None else "unknown"
        print(f"Page {page.page} · total: {total}")

    def show_lists(lists):
        if not lists:
            print("No booklists found.")
        for item in lists:
            count = item.book_count if item.book_count is not None else "unknown"
            visibility = "unknown" if item.is_public is None else "public" if item.is_public else "private"
            print(f"{item.booklist_id} · {item.title} · {count} books · {visibility}")

    if args.command == "favorites" and args.favorite_action in ("add", "remove"):
        result = client.set_favorite(args.reference, saved=args.favorite_action == "add")
        if args.json:
            print(json.dumps(result.to_dict()))
        else:
            state = "Added to" if result.is_favorite else "Removed from"
            print(f"{state} favorites: {result.book_id}")
        return

    if args.command == "library":
        overview = client.get_library(limit=args.limit)
        if args.json:
            print(json.dumps(overview.to_dict(), ensure_ascii=False))
        else:
            for title, page in (("Favorites", overview.favorites), ("Download history", overview.history)):
                print(title)
                show_page(page)
            print("Booklists")
            show_lists(overview.booklists)
        return
    if args.command == "booklists" and args.booklist_action != "show":
        lists = client.get_booklists()
        if args.json:
            print(json.dumps([item.to_dict() for item in lists], ensure_ascii=False))
        else:
            show_lists(lists)
        return
    if args.command == "booklists":
        page = client.get_booklist_books(args.booklist_id, page=args.page)
    else:
        fetch = client.get_favorites if args.command == "favorites" else client.get_history
        page = fetch(page=args.page, limit=args.limit, order=args.order)
    if args.json:
        print(json.dumps(page.to_dict(), ensure_ascii=False))
    else:
        show_page(page)
