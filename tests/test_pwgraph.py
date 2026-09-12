import io
import unittest

from audiorouter.pwgraph import Graph, iter_dump_blocks

from . import fakes


def build() -> Graph:
    return Graph(
        [
            fakes.sink(50, "alsa_output.pci-0000_0f_00.6.analog-stereo", serial=500,
                       description="Built-in Audio"),
            fakes.sink(51, "ar_headphones", serial=510, hardware=False,
                       description="Headphones", **{"audiorouter.channel": "headphones"}),
            fakes.sink(52, "easyeffects_sink", serial=520, hardware=False),
            fakes.sink(53, "easyeffects_sink", serial=530, hardware=False),
            fakes.stream(60, "firefox", serial=600, client_id=70,
                         **{"application.process.binary": "firefox",
                            "media.name": "Some Video"}),
            fakes.node(61, "ar_headphones_out", "Stream/Output/Audio", serial=610,
                       **{"audiorouter.channel": "headphones"}),
            fakes.port(80, 60, "out"),
            fakes.port(81, 50, "in"),
            fakes.link(90, 80, 81),
            fakes.client(70, 4242),
        ]
    )


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.graph = build()

    def test_devices_are_hardware_backed_sinks_only(self):
        self.assertEqual([n.name for n in self.graph.devices()],
                         ["alsa_output.pci-0000_0f_00.6.analog-stereo"])

    def test_sinks_include_virtual_ones(self):
        self.assertEqual(len(self.graph.sinks()), 4)

    def test_our_own_chain_output_is_not_an_app_stream(self):
        # A filter-chain's playback node has the same media.class as an app
        # stream; routing it would feed a channel back into itself.
        self.assertEqual([n.name for n in self.graph.app_streams()], ["firefox"])
        self.assertTrue(self.graph.node(61).is_ours)
        self.assertEqual(self.graph.node(61).owned_channel, "headphones")

    def test_duplicate_names_are_never_resolved(self):
        self.assertEqual(len(self.graph.nodes_named("easyeffects_sink")), 2)
        self.assertIsNone(self.graph.unique_node_named("easyeffects_sink"))
        self.assertIsNotNone(self.graph.unique_node_named("ar_headphones"))

    def test_missing_name_resolves_to_nothing(self):
        self.assertIsNone(self.graph.unique_node_named("ar_nope"))

    def test_stream_destination_comes_from_links(self):
        sink = self.graph.sink_of_stream(60)
        self.assertIsNotNone(sink)
        self.assertEqual(sink.id, 50)

    def test_unlinked_stream_has_no_sink(self):
        self.assertIsNone(self.graph.sink_of_stream(61))

    def test_sink_can_be_found_by_the_pid_that_owns_it(self):
        graph = Graph([fakes.sink(55, "easyeffects_sink", serial=550, hardware=False,
                                  client_id=71), fakes.client(71, 999)])
        found = graph.sink_owned_by_pid(999)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, 55)

    def test_app_label_prefers_the_friendly_name(self):
        node = self.graph.node(60)
        self.assertEqual(node.app_name, "firefox")
        self.assertEqual(node.binary, "firefox")
        self.assertEqual(node.media_name, "Some Video")

    def test_serial_is_read_but_ids_stay_separate(self):
        node = self.graph.unique_node_named("ar_headphones")
        self.assertEqual((node.id, node.serial), (51, 510))

    def test_removal_events_delete_objects(self):
        self.graph.apply([fakes.removal(60)])
        self.assertEqual(self.graph.app_streams(), [])
        self.assertIsNone(self.graph.node(60))

    def test_updates_replace_objects_in_place(self):
        self.graph.apply([fakes.stream(60, "firefox", serial=601, description="renamed")])
        self.assertEqual(self.graph.node(60).serial, 601)
        self.assertEqual(len(self.graph.nodes), 6)


class DumpStreamTest(unittest.TestCase):
    def test_each_array_is_yielded_separately(self):
        text = '[\n  {"id": 1}\n]\n[\n  {"id": 2}\n]\n'
        blocks = list(iter_dump_blocks(io.StringIO(text)))
        self.assertEqual(blocks, [[{"id": 1}], [{"id": 2}]])

    def test_noise_before_the_first_array_is_ignored(self):
        text = 'warning: something\n[\n  {"id": 1}\n]\n'
        self.assertEqual(list(iter_dump_blocks(io.StringIO(text))), [[{"id": 1}]])

    def test_a_truncated_final_block_is_not_yielded(self):
        text = '[\n  {"id": 1}\n]\n[\n  {"id": 2}\n'
        self.assertEqual(list(iter_dump_blocks(io.StringIO(text))), [[{"id": 1}]])


if __name__ == "__main__":
    unittest.main()
