from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import polars as pl
import pytest
from freezegun import freeze_time
from polars.testing import assert_frame_equal, assert_frame_not_equal

from cladetime import CladeTime, sequence
from cladetime.exceptions import CladeTimeSequenceWarning
from cladetime.util.reference import _docker_installed

docker_enabled = _docker_installed()


@pytest.mark.skipif(not docker_enabled, reason="Docker is not installed")
def test_cladetime_assign_clades(tmp_path, demo_mode):
    # demo_mode fixture overrides CladeTime config to use Nextstrain's 100k sample
    # sequence and sequence metadata instead of the entire universe of SARS-CoV-2 sequences
    assignment_file = tmp_path / "assignments.tsv"

    with freeze_time("2024-11-01"):
        ct = CladeTime()

        metadata_filtered = sequence.filter_metadata(ct.sequence_metadata, collection_min_date="2024-10-01")

        # store clade assignments as they exist on the metadata file downloaded from Nextstrain
        original_clade_assignments = metadata_filtered.select(["strain", "clade"])

        # assign clades to the same sequences using cladetime
        assigned_clades = ct.assign_clades(metadata_filtered, output_file=assignment_file)

        # clade assignments via cladetime should match the original clade assignments
        check_clade_assignments = original_clade_assignments.join(
            assigned_clades.detail, on=["strain", "clade"]
        ).collect()
        assert len(check_clade_assignments) == len(metadata_filtered.collect())
        unmatched_clade_count = check_clade_assignments.filter(pl.col("clade").is_null()).shape[0]
        assert unmatched_clade_count == 0

        # summarized clade assignments should also match summarized clade assignments from the
        # original metadata file
        assert_frame_equal(
            sequence.summarize_clades(metadata_filtered.rename({"clade": "clade_nextstrain"})),
            assigned_clades.summary,
            check_column_order=False,
            check_row_order=False,
        )

        # metadata should reflect ncov metadata as of 2024-11-01
        assert assigned_clades.meta.get("sequence_as_of") == datetime(2024, 11, 1, tzinfo=timezone.utc)
        assert assigned_clades.meta.get("tree_as_of") == datetime(2024, 11, 1, tzinfo=timezone.utc)
        assert assigned_clades.meta.get("nextclade_dataset_version") == "2024-10-17--16-48-48Z"
        assert assigned_clades.meta.get("nextclade_version_num") == "3.9.1"
        assert assigned_clades.meta.get("assignment_as_of") == "2024-11-01 00:00"


@pytest.mark.skipif(not docker_enabled, reason="Docker is not installed")
def test_assign_old_tree(test_file_path, tmp_path, test_sequences):
    sequence_file, sequence_set = test_sequences
    sequence_list = list(sequence_set)
    sequence_list.sort()

    fasta_mock = MagicMock(return_value=test_file_path / sequence_file, name="cladetime.sequence.filter")
    test_filtered_metadata = {
        "country": ["USA", "USA", "USA"],
        "date": ["2022-01-02", "2022-01-02", "2023-02-01"],
        "host": ["Homo sapiens", "Homo sapiens", "Homo sapiens"],
        "location": ["Hawaii", "Hawaii", "Utah"],
        "strain": sequence_list,
    }
    metadata_filtered = pl.LazyFrame(test_filtered_metadata)

    # expected clade assignments for 2024-08-02 (as retrieved from Nextrain metadata)
    expected_assignment_dict = {
        "strain": ["USA/VA-CDC-LC1109961/2024", "USA/FL-CDC-LC1109983/2024", "USA/MD-CDC-LC1110088/2024"],
        "clade": ["24C", "24B", "24B"],
    }
    expected_assignments = pl.DataFrame(expected_assignment_dict)

    with freeze_time("2024-11-01"):
        current_file = tmp_path / "current_assignments.tsv"
        ct_current_tree = CladeTime()
        with patch("cladetime.sequence.filter", fasta_mock):
            current_assigned_clades = ct_current_tree.assign_clades(metadata_filtered, output_file=current_file)
            current_assigned_clades = current_assigned_clades.detail.select(["strain", "clade"]).collect()

        old_file = tmp_path / "old_assignments.tsv"
        ct_old_tree = CladeTime(tree_as_of="2024-08-02")
        with patch("cladetime.sequence.filter", fasta_mock):
            old_assigned_clades = ct_old_tree.assign_clades(metadata_filtered, output_file=old_file)
            old_assigned_clade_detail = old_assigned_clades.detail.select(["strain", "clade"]).collect()

    assert_frame_equal(current_assigned_clades.select("strain"), old_assigned_clade_detail.select("strain"))
    assert_frame_not_equal(current_assigned_clades.select("clade"), old_assigned_clade_detail.select("clade"))
    assert_frame_equal(old_assigned_clade_detail.sort("strain"), expected_assignments.sort("strain"))

    expected_summary = pl.DataFrame(
        {
            "clade_nextstrain": ["24B", "24C"],
            "country": ["USA", "USA"],
            "date": ["2022-01-02", "2023-02-01"],
            "host": ["Homo sapiens", "Homo sapiens"],
            "location": ["Hawaii", "Utah"],
            "count": [2, 1],
        }
    ).cast({"count": pl.UInt32})
    assert_frame_equal(
        expected_summary, old_assigned_clades.summary.collect(), check_column_order=False, check_row_order=False
    )

    assert old_assigned_clades.meta.get("sequence_as_of") == datetime(2024, 11, 1, tzinfo=timezone.utc)
    assert old_assigned_clades.meta.get("tree_as_of") == datetime(2024, 8, 2, 11, 59, 59, tzinfo=timezone.utc)
    # nextclade metadata should reflect its state on tree_as_of (2024-08-02)
    assert old_assigned_clades.meta.get("nextclade_dataset_version") == "2024-07-17--12-57-03Z"
    assert old_assigned_clades.meta.get("nextclade_version_num") == "3.8.2"
    assert old_assigned_clades.meta.get("assignment_as_of") == "2024-11-01 00:00"

@pytest.mark.skipif(not docker_enabled, reason="Docker is not installed")
@pytest.mark.parametrize("sequence_file", ["test_sequences.fasta.xz", "test_sequences.fasta.zst"])
def test_assign_clade_detail(test_file_path, tmpdir, sequence_file):
    """Test the final clade assignment linefile."""
    test_sequence_file = test_file_path / sequence_file

    # The list below represents sequences in the test fasta file AND test metadata file
    expected_sequence_assignments = {
        "USA/WV064580/2020": "20G"
    }

    mock_download = MagicMock(return_value=test_sequence_file, name="_download_from_url_mock")
    with patch("cladetime.sequence._download_from_url", mock_download):
        ct = CladeTime()
        ct.url_sequence = test_sequence_file.as_uri()  # used to determine extension when reading .fasta file
        test_metadata = pl.read_csv(test_file_path / "metadata.tsv.zst", separator="\t", infer_schema_length=100000) \
            .filter(pl.col("strain").is_in(expected_sequence_assignments.keys())) \
            .lazy()

        clades = ct.assign_clades(test_metadata, output_file=tmpdir / "assignments.tsv")
        detailed_df = clades.detail.collect()

        # assign_clades detail output should have the same number of records as the input metadata
        assert len(detailed_df) == len(expected_sequence_assignments)

        # check actual clade assignments against expected assignments
        strain_clade_dict = detailed_df.select("strain", "clade_nextstrain").to_dicts()
        for item in strain_clade_dict:
            assert expected_sequence_assignments[item["strain"]] == item["clade_nextstrain"]

        # check metadata
        assert clades.meta.get("sequences_to_assign") == 1
        assert clades.meta.get("sequences_assigned") == 1


@pytest.mark.skipif(not docker_enabled, reason="Docker is not installed")
@pytest.mark.parametrize("sequence_file", ["test_sequences.fasta.xz", "test_sequences.fasta.zst"])
def test_assign_clade_detail_missing_assignments(test_file_path, tmpdir, sequence_file):
    """Test the final clade assignment linefile when some sequences are not assigned clades."""
    test_sequence_file = test_file_path / sequence_file

    mock_download = MagicMock(return_value=test_sequence_file, name="_download_from_url_mock")
    with patch("cladetime.sequence._download_from_url", mock_download):
        ct = CladeTime()
        ct.url_sequence = test_sequence_file.as_uri()  # used to determine extension when reading .fasta file
        test_metadata = pl.read_csv(test_file_path / "metadata.tsv.zst", separator="\t", infer_schema_length=100000).lazy()
        test_metadata_count = len(test_metadata.collect())

        clades = ct.assign_clades(test_metadata, output_file=tmpdir / "assignments.tsv")
        detailed_df = clades.detail.collect()

        # assign_clades detail output should have the same number of records as the input metadata
        assert len(detailed_df) == test_metadata_count

        # check metadata
        # only one sequence in the test metadata is in the test fasta
        assert clades.meta.get("sequences_to_assign") == test_metadata_count
        assert clades.meta.get("sequences_assigned") == 1


@pytest.mark.skipif(not docker_enabled, reason="Docker is not installed")
@pytest.mark.parametrize(
    "min_date, max_date, expected_rows",
    [("2023-12-27", None, 1), (None, "2022-01-03", 2), ("2022-01-02", "2023-12-28", 2)],
)
def test_assign_date_filters(test_file_path, tmp_path, test_sequences, min_date, max_date, expected_rows):
    sequence_file, sequence_set = test_sequences
    fasta_mock = MagicMock(return_value=test_file_path / sequence_file, name="cladetime.sequence.filter")
    test_metadata = {
        "date": ["2022-01-01", "2022-01-03", "2023-12-27"],
        "strain": list(sequence_set),
        "clade_nextstrain": ["11C", "11B", "11B"],
        "host": ["Homo sapiens", "Homo sapiens", "Homo sapiens"],
        "country": ["USA", "USA", "USA"],
        "division": ["Utah", "Utah", "Utah"],
        "wombat_count": [2, 22, 222],
    }
    metadata = pl.LazyFrame(test_metadata)
    metadata_filtered = sequence.filter_metadata(
        metadata=metadata, collection_min_date=min_date, collection_max_date=max_date
    )

    ct = CladeTime()
    assignment_file = tmp_path / "assignments.tsv"
    with patch("cladetime.sequence.filter", fasta_mock):
        assigned_clades = ct.assign_clades(metadata_filtered, output_file=assignment_file)
    assert len(assigned_clades.detail.collect()) == expected_rows


@pytest.mark.skipif(not docker_enabled, reason="Docker is not installed")
def test_assign_too_many_sequences_warning(tmp_path, test_file_path, test_sequences):
    sequence_file, sequence_set = test_sequences

    ct = CladeTime()
    ct._config.clade_assignment_warning_threshold = 2
    test_filtered_metadata = {"date": ["2022-01-01", "2022-01-02", "2023-12-27"], "strain": ["aa", "bb", "cc"]}
    metadata_filtered = pl.LazyFrame(test_filtered_metadata)
    fasta_mock = MagicMock(return_value=test_file_path / sequence_file, name="cladetime.sequence.filter")
    with patch("cladetime.sequence.filter", fasta_mock):
        with pytest.warns(CladeTimeSequenceWarning):
            assignments = ct.assign_clades(metadata_filtered, output_file=tmp_path / "assignments.tsv")
            # clade assignment should proceed, despite the warning
            assert len(assignments.detail.collect()) == 3


@pytest.mark.parametrize("empty_input", [(pl.LazyFrame()), (pl.DataFrame()), (pl.DataFrame({"strain": []}))])
def test_assign_clades_no_sequences(empty_input):
    ct = CladeTime()
    with pytest.warns(CladeTimeSequenceWarning):
        assignments = ct.assign_clades(empty_input)
        assert assignments.detail.collect().shape == (0, 0)
        assert assignments.summary.collect().shape == (0, 0)
        assert assignments.meta == {}
