import subprocess
import sys
import threading
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "drop"


def run(service, home, *args):
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "DROP_URL": service.url},
    )


def test_latest_jpg_always_exists_after_an_image_pull(service, tmp_path):
    service.add(variants=("orig", "view_png", "view_jpg"))
    run(service, tmp_path, "pull")
    assert (tmp_path / "drop" / "latest.jpg").exists()


def test_latest_png_accompanies_a_lossless_image(service, tmp_path):
    service.add(variants=("orig", "view_png", "view_jpg"))
    run(service, tmp_path, "pull")
    assert (tmp_path / "drop" / "latest.png").exists()


def test_a_stale_latest_png_is_removed_when_the_newest_image_is_lossy(service,
                                                                      tmp_path):
    service.add(filename="screenshot.png", variants=("orig", "view_png", "view_jpg"))
    run(service, tmp_path, "pull")
    assert (tmp_path / "drop" / "latest.png").exists()

    service.add(filename="photo.jpg", variants=("orig", "view_jpg"),
                media_type="image/jpeg")
    run(service, tmp_path, "pull")
    # The skill tells Claude to prefer latest.png; leaving the old one would
    # make it analyse the wrong screenshot.
    assert not (tmp_path / "drop" / "latest.png").exists()
    assert (tmp_path / "drop" / "latest.jpg").exists()


def test_latest_is_not_updated_by_a_raw_drop(service, tmp_path):
    service.add(filename="a.png", variants=("orig", "view_png", "view_jpg"))
    run(service, tmp_path, "pull")
    before = (tmp_path / "drop" / "latest.jpg").read_bytes()

    service.add(filename="notes.jpg", kind="raw", variants=("orig",),
                media_type="text/plain")
    run(service, tmp_path, "pull")
    assert (tmp_path / "drop" / "latest.jpg").read_bytes() == before


def test_batch_directory_is_numbered_chronologically(service, tmp_path):
    service.add(filename="a.png")
    service.add(filename="b.png")
    run(service, tmp_path, "pull")
    names = sorted(p.name for p in (tmp_path / "drop" / "latest").iterdir())
    assert names[0].startswith("01-") and names[1].startswith("02-")
    assert len(names) == 2                      # one entry per drop


def test_an_empty_pull_leaves_latest_untouched(service, tmp_path):
    service.add()
    run(service, tmp_path, "pull")
    before = (tmp_path / "drop" / "latest.jpg").read_bytes()
    run(service, tmp_path, "pull")
    assert (tmp_path / "drop" / "latest.jpg").read_bytes() == before


def test_a_companion_download_failure_changes_nothing(service, tmp_path):
    """The companion for latest.jpg is downloaded during staging, before
    anything is published — so its failure must leave the whole previous
    state intact, batch directory and latest.* alike."""
    service.add(filename="first.png")
    run(service, tmp_path, "pull")
    before_batch = sorted(p.name for p in (tmp_path / "drop" / "latest").iterdir())
    before_jpg = (tmp_path / "drop" / "latest.jpg").read_bytes()

    service.add(filename="second.png")
    service.fail_downloads = True
    assert run(service, tmp_path, "pull").returncode == 1

    assert sorted(p.name for p in
                  (tmp_path / "drop" / "latest").iterdir()) == before_batch
    assert (tmp_path / "drop" / "latest.jpg").read_bytes() == before_jpg


def test_a_truncated_download_is_a_failure_not_a_published_file(service, tmp_path):
    """A cut connection produces a short body that looks exactly like a small
    file. Content-Length is what tells them apart."""
    service.truncate_downloads = True
    service.add(filename="first.png")
    assert run(service, tmp_path, "pull").returncode == 1
    assert not (tmp_path / "drop" / "latest.jpg").exists()


def test_a_crashed_pull_leaves_no_debris_behind(service, tmp_path):
    service.add()
    run(service, tmp_path, "pull")
    litter = tmp_path / "drop" / ".staging-crashed"
    litter.mkdir()
    (litter / "junk").write_bytes(b"x")
    (tmp_path / "drop" / ".latest.old").mkdir()

    service.add()
    run(service, tmp_path, "pull")
    leftovers = [p.name for p in (tmp_path / "drop").iterdir()
                 if p.name.startswith(".staging-") or p.name == ".latest.old"]
    assert leftovers == []


def test_staging_lives_inside_drop_so_moves_stay_on_one_filesystem(service,
                                                                   tmp_path):
    # Under ~/.local/state a move into a separately mounted ~/drop becomes a
    # copy, which is neither atomic nor cheap.
    service.add()
    run(service, tmp_path, "pull")
    state = tmp_path / ".local" / "state" / "claude-drop"
    assert not list(state.glob("staging-*"))


def test_an_orig_pull_keeps_a_real_extension(service, tmp_path):
    service.add(media_type="image/jpeg", variants=("orig", "view_jpg"),
                filename="photo.jpg")
    path = Path(run(service, tmp_path, "pull", "--full").stdout.split()[0])
    assert path.suffix == ".jpg"


def test_a_full_pull_of_a_lossless_image_keeps_its_png_companion(service,
                                                                 tmp_path):
    # Companions come from variant metadata, not from the local suffix: a
    # --full pull writes a .jpg file, but the drop still has view_png.
    service.add(media_type="image/tiff",
                variants=("orig", "view_png", "view_jpg", "full_jpg"))
    run(service, tmp_path, "pull", "--full")
    assert (tmp_path / "drop" / "latest.png").exists()
    assert (tmp_path / "drop" / "latest.jpg").exists()


def test_immutable_names_carry_the_variant_so_full_does_not_overwrite(service,
                                                                      tmp_path):
    drop = service.add(media_type="image/heic",
                       variants=("orig", "view_jpg", "full_jpg"))
    normal = run(service, tmp_path, "pull").stdout.split()[0]
    full = run(service, tmp_path, "pull", "--id", drop["id"],
               "--full").stdout.split()[0]
    assert normal != full
    assert Path(normal).exists() and Path(full).exists()


def test_filenames_with_spaces_are_sanitised(service, tmp_path):
    service.add(filename="Bildschirmfoto 2026-09-11 um 14.32.png")
    path = Path(run(service, tmp_path, "pull").stdout.splitlines()[0])
    assert " " not in path.name
    assert path.exists()


def test_non_ascii_filenames_are_sanitised(service, tmp_path):
    service.add(filename="Straßenbahn-Störung-Übersicht.png")
    path = Path(run(service, tmp_path, "pull").stdout.splitlines()[0])
    assert path.name.isascii()
    assert path.exists()


def test_a_newline_in_a_filename_cannot_break_the_output_contract(service,
                                                                  tmp_path):
    service.add(filename="evil\nname.png")
    out = run(service, tmp_path, "pull").stdout
    assert len([line for line in out.splitlines() if line]) == 1


def test_traversal_in_a_filename_cannot_escape_the_drop_directory(service,
                                                                  tmp_path):
    service.add(filename="../../../../tmp/evil.png")
    path = Path(run(service, tmp_path, "pull").stdout.splitlines()[0]).resolve()
    assert path.is_relative_to((tmp_path / "drop").resolve())


def test_two_concurrent_pulls_do_not_interleave(service, tmp_path):
    for _ in range(4):
        service.add()
    results, lock = [], threading.Lock()

    def worker():
        result = run(service, tmp_path, "pull")
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.returncode == 0 for r in results)
    # Exactly one of them gets the drops; the other finds nothing new.
    counts = sorted(len(r.stdout.split()) for r in results)
    assert counts == [0, 4]
