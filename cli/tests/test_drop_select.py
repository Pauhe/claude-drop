import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "drop"


def run(service, home, *args):
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "DROP_URL": service.url},
    )


def test_pull_prints_one_absolute_path_per_drop(service, tmp_path):
    service.add()
    result = run(service, tmp_path, "pull")
    assert result.returncode == 0, result.stderr
    paths = [p for p in result.stdout.splitlines() if p]
    assert len(paths) == 1                       # one path per drop, not per variant
    assert Path(paths[0]).is_absolute()


def test_two_drops_give_two_paths(service, tmp_path):
    service.add(filename="a.png")
    service.add(filename="b.png")
    out = run(service, tmp_path, "pull").stdout
    assert len([p for p in out.splitlines() if p]) == 2


def test_the_primary_variant_is_the_lossless_one_when_present(service, tmp_path):
    service.add(variants=("orig", "view_png", "view_jpg"))
    path = run(service, tmp_path, "pull").stdout.split()[0]
    assert path.endswith(".png")


def test_the_primary_variant_falls_back_to_jpeg(service, tmp_path):
    service.add(variants=("orig", "view_jpg"))
    path = run(service, tmp_path, "pull").stdout.split()[0]
    assert path.endswith(".jpg")


def test_full_uses_the_full_variant_when_the_original_is_unreadable(service,
                                                                    tmp_path):
    service.add(media_type="image/heic", variants=("orig", "view_jpg", "full_jpg"))
    run(service, tmp_path, "pull", "--full")
    assert any("full_jpg" in p for p in service.paths)


def test_full_uses_the_original_when_it_is_already_readable(service, tmp_path):
    service.add(media_type="image/jpeg", variants=("orig", "view_jpg"))
    run(service, tmp_path, "pull", "--full")
    assert any(p.endswith("/orig") for p in service.paths)


def test_full_never_downloads_a_2000px_view(service, tmp_path):
    service.add(media_type="image/heic", variants=("orig", "view_jpg", "full_jpg"))
    run(service, tmp_path, "pull", "--full")
    assert not any(p.endswith("/view_jpg") for p in service.paths)


def test_second_pull_returns_nothing_new(service, tmp_path):
    service.add()
    run(service, tmp_path, "pull")
    assert run(service, tmp_path, "pull").stdout.strip() == ""


def test_there_is_no_first_run_window(service, tmp_path):
    # A drop from three hours ago must still be fetched by a first pull.
    service.add(minutes_ago=180)
    assert len(run(service, tmp_path, "pull").stdout.split()) == 1


def test_pull_pages_through_everything_before_advancing(service, tmp_path):
    for _ in range(5):
        service.add()
    result = run(service, tmp_path, "pull", "--page-size", "2")
    assert len(result.stdout.split()) == 5, result.stderr
    assert run(service, tmp_path, "pull").stdout.strip() == ""


def test_paging_pins_the_snapshot(service, tmp_path):
    for _ in range(3):
        service.add()
    run(service, tmp_path, "pull", "--page-size", "2")
    assert any("max_seq=" in p for p in service.paths)


def test_batch_selection_does_not_touch_the_cursor(service, tmp_path):
    service.add(batch="alpha")
    service.add(batch="beta")
    run(service, tmp_path, "pull", "--batch", "beta")
    assert len(run(service, tmp_path, "pull").stdout.split()) == 2


def test_since_does_not_touch_the_cursor(service, tmp_path):
    service.add()
    run(service, tmp_path, "pull", "--since", "1h")
    assert len(run(service, tmp_path, "pull").stdout.split()) == 1


def test_list_never_touches_the_cursor(service, tmp_path):
    service.add()
    run(service, tmp_path, "list")
    assert len(run(service, tmp_path, "pull").stdout.split()) == 1


def test_list_prints_ids_that_the_id_flag_accepts(service, tmp_path):
    drop = service.add()
    printed = run(service, tmp_path, "list").stdout.split()[0]
    assert printed == drop["id"]
    assert run(service, tmp_path, "pull", "--id", printed).returncode == 0


def test_a_download_failure_does_not_advance_the_cursor(service, tmp_path):
    service.add()
    service.fail_downloads = True
    assert run(service, tmp_path, "pull").returncode == 1
    service.fail_downloads = False
    assert len(run(service, tmp_path, "pull").stdout.split()) == 1


def test_an_unreachable_service_exits_1(tmp_path):
    class Dead:
        url = "http://127.0.0.1:1"
        paths: list = []
    assert run(Dead(), tmp_path, "pull").returncode == 1


def test_an_impossible_time_is_a_usage_error(service, tmp_path):
    result = run(service, tmp_path, "pull", "--since", "25:00")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_a_future_time_is_a_usage_error(service, tmp_path):
    assert run(service, tmp_path, "pull", "--since", "2099-01-01").returncode == 2


def test_last_zero_is_a_usage_error(service, tmp_path):
    assert run(service, tmp_path, "pull", "--last", "0").returncode == 2


def test_last_negative_is_a_usage_error(service, tmp_path):
    assert run(service, tmp_path, "pull", "--last", "-3").returncode == 2


def test_print0_separates_with_nul(service, tmp_path):
    service.add()
    assert "\0" in run(service, tmp_path, "pull", "--print0").stdout


def test_unconfigured_url_is_a_usage_error(tmp_path):
    # No built-in service URL: without DROP_URL or a config file the CLI must
    # refuse plainly rather than guess a host.
    result = subprocess.run(
        [sys.executable, str(CLI), "pull"], capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "DROP_URL" in result.stderr
    assert not (tmp_path / "drop").exists()


def test_config_file_supplies_the_url(service, tmp_path):
    service.add()
    config = tmp_path / ".config" / "claude-drop"
    config.mkdir(parents=True)
    (config / "config").write_text(f"url={service.url}\n")
    result = subprocess.run(
        [sys.executable, str(CLI), "pull"], capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr


def test_malformed_url_is_a_usage_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(CLI), "pull"], capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "DROP_URL": "drop.example"},
    )
    assert result.returncode == 2
    assert "http" in result.stderr
