#import "@preview/typslides:1.2.6": *
#import "@preview/tablem:0.2.0": tablem, three-line-table

// Project configuration
#show: typslides.with(
  ratio: "16-9",
  theme: "bluey",
)
#set text(font: ("Times New Roman Cyr", "SimHei"), lang: "zh", region: "cn")

// The front slide is the first slide of your presentation
#front-slide(
  title: "Title",
  subtitle: [Sub Title],
  authors: "authors",
  info: [information],
)

#set grid(
  columns: 2,
  inset: 15pt,
  stroke: (x, y) => if x > 0 {
    (
      left: (
        paint: luma(180),
        thickness: 1.5pt,
        dash: "dotted",
      ),
    )
  },
)

// Custom outline
#table-of-contents()

// Title slides create new sections
#title-slide[
  Background
]

#slide(title: "Background: Problem Description")[ ]


#focus-slide[
  Thanks for listening!
]
